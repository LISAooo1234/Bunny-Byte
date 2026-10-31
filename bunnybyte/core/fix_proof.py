"""修复有效性证明：验证通过之后，在临时副本里撤回本轮的源码改动、保留改过的测试，再跑一遍这些测试。

测试在旧代码上失败、在新代码上通过，才说明它真的测到了这次改动（PROVEN）；撤回后照样通过，说明
测试没碰到改动（THEATER）；旧代码上连导入都不行，说明测试依赖本轮新增的接口，行为测没测到无法判断
（WEAK）。全程不碰用户的工作区，也不调用模型。接入验证闸门的部分在 fix_proof_gate.py。
"""

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from ..features.sandbox.process import run_command
from .shadow_snapshots import _LOCK, BLOB_BATCH
from .verification import TEST_FILE_RE, _all_assert_scripts, _uses_pytest

PROVEN = "proven"
THEATER = "theater"
WEAK = "weak"
SKIPPED = "skipped"

MAX_FILES = 5000
MAX_BYTES = 200_000_000
MAX_TESTS = 20
OUTPUT_TAIL_CHARS = 1500
# 撤回这些文件会改变测试怎么被收集（--strict-markers、pythonpath、依赖），得到的“失败”和代码行为无关
KEEP_FILES = {
    "pyproject.toml", "setup.cfg", "setup.py", "pytest.ini", "tox.ini", ".coveragerc",
    "uv.lock", "poetry.lock", "Pipfile", "Pipfile.lock", "conftest.py",
}
DOC_SUFFIXES = {".md", ".rst"}
TEST_DIRS = {"tests", "test"}
ASSERTION_LINE_RE = re.compile(
    r"^\s*(?:async\s+)?def\s+test|\bassert|\.assert[A-Z]\w*\(|\bpytest\.(?:raises|param)\b|\bself\.fail\(|parametrize"
    r"|^\s*[(\[].*[)\]],?\s*(?:#.*)?$"  # parametrize 的数据行：补一行 (2, 3, 5) 就是补了一个回归用例
)
# 旧代码上“没有这个接口”的报错：测试依赖本轮新增的函数 / 方法 / 参数，说明不了行为。'NoneType' 那种是真崩溃，不算
WEAK_RE = re.compile(
    r"ImportError|ModuleNotFoundError|cannot import name|module '[^']+' has no attribute"
    r"|'(?!NoneType')\w+' object has no attribute|got an unexpected keyword argument"
    r"|takes \d+ positional arguments? but \d+ (?:was|were) given"
)
NO_TESTS_RE = re.compile(r"Ran 0 tests|NO TESTS RAN|collected 0 items|no tests ran", re.IGNORECASE)
NO_PYTEST_RE = re.compile(r"No module named ['\"]?pytest")
ASSERT_FAILURE_RE = re.compile(r"AssertionError|^FAIL:", re.MULTILINE)

# 每个回合都会碰到的情况，不出卡片、不出提示，只记在 report.json 里
SILENT_SKIPS = {"no_tests", "no_sources", "no_python_sources", "tests_unchanged", "sandbox", "platform", "no_snapshot", "symlink", "aborted"}
SKIP_TEXT = {
    "too_large": f"仓库超过 {MAX_FILES} 个文件或 {MAX_BYTES // 1_000_000}MB，不复制",
    "no_pytest": "当前 Python 环境里没有 pytest",
    "no_tests_collected": "改过的测试文件里一个测试都没收集到",
    "control_failed": "改过的测试在临时副本里（新代码上）就没通过：可能测试本身失败，也可能依赖被 .gitignore 忽略、没进快照的文件",
    "control_timeout": "改过的测试在临时副本里（新代码上）超时",
    "imported_outside": "测试导入的是装在别处的代码（可编辑安装？），撤回副本里的源码证明不了什么",
    "unexpected_exit": "撤回后 pytest 异常退出，结果不能当证据",
    "error": "复制或运行时出错",
    "probe_failed": "没能记下测试导入了哪些模块，无法确认测的是副本里的代码",
}

PROBE_SOURCE = '''"""bunny 修复证明：进程退出时记下每个模块是从哪个文件导入的。"""
import atexit
import json
import os
import sys


def _bunny_fix_proof_dump():
    target = os.environ.get("BUNNY_FIX_PROOF_MODULES")
    if not target:
        return
    found = {}
    for name, module in list(sys.modules.items()):
        path = getattr(module, "__file__", None)
        if isinstance(path, str):
            found[name] = os.path.abspath(path)
    try:
        with open(os.path.join(target, "modules-%d.json" % os.getpid()), "w", encoding="utf-8") as handle:
            json.dump(found, handle)
    except OSError:
        pass


atexit.register(_bunny_fix_proof_dump)


def _bunny_chain_project_sitecustomize():
    """探针排在 PYTHONPATH 最前面，会挡住项目自己的 sitecustomize.py：找到它并照常执行。"""
    import importlib.machinery

    here = os.path.dirname(os.path.abspath(__file__))
    rest = [entry for entry in sys.path if os.path.abspath(entry or ".") != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is not None and spec.origin and os.path.abspath(spec.origin) != os.path.abspath(__file__):
        with open(spec.origin, encoding="utf-8") as handle:
            exec(compile(handle.read(), spec.origin, "exec"), {"__name__": "sitecustomize", "__file__": spec.origin})


_bunny_chain_project_sitecustomize()
'''


@dataclass
class _Outcome:
    code: int | None
    output: str = ""
    seconds: float = 0.0
    timed_out: bool = False
    stopped: bool = False
    junit: tuple | None = None  # (测试数, 失败信息列表)

    @property
    def passed(self):
        return self.code == 0 and not self.timed_out and not self.stopped


def skipped(reason, **extra):
    return {"status": SKIPPED, "reason": reason, **extra}


def is_test_path(path):
    path = str(path)
    parts = Path(path).parts
    return bool(TEST_FILE_RE.search(path)) or Path(path).name == "conftest.py" or any(part in TEST_DIRS for part in parts[:-1])


def _module_tail(path):
    path = str(path)
    return path[4:] if path.startswith("src/") else path


def _assertions_changed(old_text, new_text):
    """(增加了断言/测试, 只删了断言)：两者都没有说明测试只是改了名字、格式或辅助代码。"""
    added = removed = False
    for line in difflib.unified_diff(old_text.splitlines(), new_text.splitlines(), lineterm="", n=0):
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+") and ASSERTION_LINE_RE.search(line[1:]):
            added = True
        elif line.startswith("-") and ASSERTION_LINE_RE.search(line[1:]):
            removed = True
    return added, removed and not added


def _interpreter(root, path=None):
    for relative in (".venv/bin/python", "venv/bin/python"):
        candidate = Path(root) / relative
        if candidate.is_file():
            return os.path.abspath(candidate)  # 不 resolve：venv 的 python 是软链接，解开后就不在 venv 里了
    return which("python3", path=path) or which("python", path=path) or sys.executable


def _read_text(store, oid):
    if not oid or set(oid) == {"0"}:
        return ""
    with _LOCK:
        data = store._read_blobs([oid]).get(oid, b"")
    return data.decode("utf-8", "replace")


class _Aborted(Exception):
    pass


def _materialize(store, entries, target, should_stop=None):
    real = os.path.realpath(target)
    items = sorted(entries.items())
    for start in range(0, len(items), BLOB_BATCH):
        if should_stop and should_stop():
            raise _Aborted()
        chunk = items[start : start + BLOB_BATCH]
        with _LOCK:
            data = store._read_blobs([oid for _, (_, oid, _) in chunk])
        for relative, (mode, oid, _size) in chunk:
            path = store._safe_target(relative, real)
            if path is not None:
                store._write_entry(path, mode, data[oid])


def _revert(store, work, sources):
    real = os.path.realpath(work)
    restore = [item for item in sources if item["status"] != "A"]
    with _LOCK:
        data = store._read_blobs([item["old_oid"] for item in restore]) if restore else {}
    for item in sources:
        path = store._safe_target(item["path"], real)
        if path is None:
            continue
        if item["status"] == "A":
            if os.path.lexists(path):
                os.unlink(path)
        else:
            store._write_entry(path, item.get("old_mode") or "100644", data[item["old_oid"]])


def _junit(path):
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return None
    total, failures = 0, []
    for case in tree.iter("testcase"):
        total += 1
        for child in case:
            if child.tag in ("failure", "error"):
                name = f"{case.get('classname', '')}::{case.get('name', '')}".strip(":")
                failures.append((name, f"{child.get('message', '')}\n{child.text or ''}"))
    return total, failures


def _loaded_modules(directory):
    found = {}
    for path in Path(directory).glob("modules-*.json"):
        try:
            found.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return found


def _wants_pytest(work, tests):
    """项目声明了 pytest，或测试是不带 unittest 的裸 `def test_` 函数（unittest 一个都收集不到）。"""
    if _uses_pytest(work):
        return True
    for relative in tests:
        try:
            text = (Path(work) / relative).read_text(encoding="utf-8", errors="replace")[:20_000]
        except OSError:
            continue
        if re.search(r"^\s*def test", text, re.MULTILINE) and "unittest" not in text:
            return True
    return False


class _Runner:
    def __init__(self, root, work, probe, modules, env, should_stop, tests=()):
        self.root, self.work, self.modules = Path(root), Path(work), Path(modules)
        self.python = _interpreter(root, env.get("PATH"))
        self.pytest = _wants_pytest(self.work, tests)
        paths = [str(probe), str(self.work)] + ([str(self.work / "src")] if (self.work / "src").is_dir() else [])
        self.env = dict(
            env,
            PYTHONPATH=os.pathsep.join(paths),
            PYTHONDONTWRITEBYTECODE="1",
            CI="1",
            PWD=str(self.work),
            BUNNY_FIX_PROOF_MODULES=str(self.modules),
        )
        self.should_stop = should_stop
        self.extra = []

    def commands(self, tests):
        if self.pytest:
            junit = str(self.modules.parent / "junit.xml")
            argv = [self.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", ".", f"--junitxml={junit}"]
            return [[*argv, *self.extra, *tests]]
        if _all_assert_scripts(self.work, tests):
            return [[self.python, test] for test in tests]
        return [[self.python, "-m", "unittest", *tests]]

    def display(self, tests):
        name = Path(self.python).name
        if self.pytest:
            return " ".join([name, "-m", "pytest", "-q", *self.extra, *tests])
        if _all_assert_scripts(self.work, tests):
            return " ; ".join(f"{name} {test}" for test in tests)
        return " ".join([name, "-m", "unittest", *tests])

    def run(self, tests, timeout):
        shutil.rmtree(self.modules, ignore_errors=True)
        self.modules.mkdir()
        junit = self.modules.parent / "junit.xml"
        if junit.exists():
            junit.unlink()
        started = time.monotonic()
        outcome = _Outcome(code=0)
        outputs = []
        for argv in self.commands(tests):
            left = timeout - (time.monotonic() - started)
            try:
                done = run_command(argv, cwd=str(self.work), env=self.env, timeout=max(1, left), should_stop=self.should_stop, shell=False)
            except subprocess.TimeoutExpired as exc:
                outputs.append(f"{exc.output or ''}{exc.stderr or ''}")
                outcome.timed_out = True
                break
            outputs.append((done.stdout or "") + (done.stderr or ""))
            if done.stopped:
                outcome.stopped = True
                break
            if done.returncode != 0:
                outcome.code = done.returncode
                break
        outcome.output = "\n".join(part.strip() for part in outputs if part.strip())
        outcome.seconds = round(time.monotonic() - started, 2)
        if self.pytest:
            outcome.junit = _junit(junit)
        return outcome


def _classify(runner, outcome):
    """撤回源码后的那次运行 -> (状态, 失败的测试名)。"""
    if outcome.timed_out:
        return PROVEN, ["(超时)"]
    if outcome.passed:
        return THEATER, []
    if runner.pytest:
        if outcome.code == 2:
            return WEAK, []  # 收集阶段就失败：几乎都是导入不了本轮新加的东西
        if outcome.code != 1:
            return None, []
        if outcome.junit and outcome.junit[1]:
            failures = outcome.junit[1]
            names = [name for name, _ in failures]
            texts = [text for _, text in failures]
            if all(WEAK_RE.search(text) and "AssertionError" not in text for text in texts):
                return WEAK, names
            return PROVEN, names
    output = outcome.output
    if WEAK_RE.search(output) and not ASSERT_FAILURE_RE.search(output):
        return WEAK, []
    return PROVEN, []


def _addopts_suspect(control):
    """新代码上就失败、而且像是 addopts 惹的：用法错误（-n auto 但没装 xdist），或覆盖率门槛只跑几个文件过不了。"""
    return control.code == 4 or bool(re.search(r"Required test coverage|--cov|addopts", control.output, re.IGNORECASE))


def _tail(text):
    text = str(text or "").strip()
    return text if len(text) <= OUTPUT_TAIL_CHARS else "…" + text[-OUTPUT_TAIL_CHARS:]


def _plan(store, before_tree, after_tree):
    """拆出要跑的测试和要撤回的源码；不值得跑时返回 skipped(...)。"""
    changes = store.diff(before_tree, after_tree)
    if any("120000" in (item["mode"], item.get("old_mode", "")) for item in changes):
        return skipped("symlink")
    tests, sources = [], []
    for item in changes:
        path = item["path"]
        if is_test_path(path):
            if item["status"] != "D" and TEST_FILE_RE.search(path):
                tests.append(item)
        elif Path(path).name not in KEEP_FILES and Path(path).suffix.lower() not in DOC_SUFFIXES and not path.startswith("docs/"):
            sources.append(item)
    if not tests:
        return skipped("no_tests")
    if not sources:
        return skipped("no_sources")
    if not any(item["path"].endswith(".py") for item in sources):
        return skipped("no_python_sources")  # 只改了测试和 coverage.xml / requirements.txt 之类，没有可撤回的代码
    removed_only = True
    meaningful = []
    for item in tests[:MAX_TESTS]:
        old = "" if item["status"] == "A" else _read_text(store, item["old_oid"])
        added, removed = _assertions_changed(old, _read_text(store, item["oid"]))
        if added or removed:
            meaningful.append(item["path"])
            removed_only = removed_only and removed
    if not meaningful:
        return skipped("tests_unchanged")
    return {"tests": meaningful, "sources": sources, "assertions_removed_only": removed_only}


def plan_fix_proof(store, before_tree, after_tree):
    """不跑任何测试的前置判断：值得证明时返回 {"tests", "sources", ...}，否则返回 skipped(...)。"""
    if os.name == "nt":
        return skipped("platform")  # 没有 killpg，超时时杀不掉测试子进程
    if not before_tree or not after_tree:
        return skipped("no_snapshot")
    plan = _plan(store, before_tree, after_tree)
    if plan.get("status") == SKIPPED:
        return plan
    entries = store.tree_entries(after_tree)
    if len(entries) > MAX_FILES or sum(size for _, _, size in entries.values()) > MAX_BYTES:
        return skipped("too_large")
    return dict(plan, entries=entries)


def prove_fix(store, root, plan, *, env, timeout, should_stop=None):
    """在临时副本里撤回本轮源码改动、重跑本轮改过的测试，返回一份修复证明（dict，可直接写进 report.json）。"""
    tests = plan["tests"]
    base = {
        "tests": tests,
        "reverted": [item["path"] for item in plan["sources"]],
        "assertions_removed_only": plan["assertions_removed_only"],
    }
    with tempfile.TemporaryDirectory(prefix="bunny-fix-proof-") as scratch:
        work, probe, modules = Path(scratch) / "work", Path(scratch) / "probe", Path(scratch) / "modules"
        for directory in (work, probe, modules):
            directory.mkdir()
        (probe / "sitecustomize.py").write_text(PROBE_SOURCE, encoding="utf-8")
        try:
            _materialize(store, plan["entries"], work, should_stop)
        except _Aborted:
            return skipped("aborted", **base)
        runner = _Runner(root, work, probe, modules, env, should_stop, tests)
        return _prove(store, runner, tests, plan["sources"], base, timeout)


def _prove(store, runner, tests, sources, base, timeout):
    result = _prove_steps(store, runner, tests, sources, base, timeout)
    if result.get("output_tail"):
        lines = [line for line in result["output_tail"].splitlines() if "generated xml file" not in line]
        text = "\n".join(lines)
        for prefix in {os.path.realpath(runner.work), str(runner.work)}:
            text = text.replace(prefix + os.sep, "")  # 临时目录的路径对用户没有意义
        result["output_tail"] = text
    return result


def _prove_steps(store, runner, tests, sources, base, timeout):
    control = runner.run(tests, timeout)
    if control.stopped:
        return skipped("aborted", **base)
    if not control.passed and runner.pytest and not control.timed_out and _addopts_suspect(control):
        runner.extra = ["-o", "addopts="]  # addopts 里的 --cov-fail-under / -n auto 只跑几个文件时会失败
        retry = runner.run(tests, timeout)
        control = retry if retry.passed else control
        if not retry.passed:
            runner.extra = []
    base["command"] = runner.display(tests)
    base["control_seconds"] = control.seconds
    if control.stopped:
        return skipped("aborted", **base)
    if NO_PYTEST_RE.search(control.output) and control.code:
        return skipped("no_pytest", **base)
    if control.code == 5 or (NO_TESTS_RE.search(control.output) and control.code in (0, 5)):
        return skipped("no_tests_collected", **base)
    if control.timed_out:
        return skipped("control_timeout", **base)
    if not control.passed:
        return skipped("control_failed", output_tail=_tail(control.output), **base)
    loaded = _loaded_modules(runner.modules)
    if not loaded:
        return skipped("probe_failed", **base)
    outside = _imported_outside(loaded, runner, sources)
    if outside:
        return skipped("imported_outside", modules=outside[:5], **base)
    python_sources = [item["path"] for item in sources if item["path"].endswith(".py") and item["status"] != "D"]
    if loaded and python_sources:
        files = {os.path.realpath(path) for path in loaded.values()}
        work = os.path.realpath(runner.work)
        base["imported"] = [path for path in python_sources if os.path.join(work, path) in files]
    _revert(store, runner.work, sources)
    reverted_timeout = min(timeout, max(10, int(control.seconds * 3) + 5))
    outcome = runner.run(tests, reverted_timeout)
    base["reverted_seconds"] = outcome.seconds
    if outcome.stopped:
        return skipped("aborted", **base)
    status, failed = _classify(runner, outcome)
    if status is None:
        return skipped("unexpected_exit", output_tail=_tail(outcome.output), **base)
    result = {"status": status, "reason": "", **base, "failed_tests": failed[:5]}
    if status in (PROVEN, WEAK):
        result["output_tail"] = _tail(outcome.output)
    return result


def _module_name(path):
    parts = list(Path(_module_tail(path)).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_outside(loaded, runner, sources):
    """改动的源码是不是从副本以外导入的：可编辑安装指回原仓库，或项目自己装进了 site-packages。"""
    root = os.path.realpath(runner.root)
    scratch = os.path.realpath(runner.work.parent)
    venvs = tuple(os.path.join(root, name) + os.sep for name in (".venv", "venv"))
    changed = {_module_name(item["path"]) for item in sources if item["path"].endswith(".py")}
    found = []
    for name, path in sorted(loaded.items()):
        real = os.path.realpath(path)
        if real.startswith(scratch + os.sep):
            continue
        from_repo = real.startswith(root + os.sep) and not real.startswith(venvs)
        if from_repo or name in changed:
            found.append(name)
    return found


def skip_text(reason):
    return SKIP_TEXT.get(reason, reason)
