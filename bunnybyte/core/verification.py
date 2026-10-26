"""验证回执：交付前真正跑一遍项目自己的检查，并留下可追溯的回执。

这里只放不依赖引擎的部分：`[verify]` 配置、检查命令探测、带超时的命令执行、
以及“回答声称已验证”的识别。接入回合循环的逻辑在 verification_gate.py。
"""

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from ..features.sandbox.checker import SandboxChecker
from ..features.sandbox.command_matcher import command_is_excluded
from .artifacts import build_artifact_graph
from .shell_segments import split_segments, stage_head
from .workspace import IGNORED_PATH_NAMES

VERIFY_MODES = ("off", "ask", "auto")
DEFAULT_TIMEOUT = 120
DEFAULT_REPAIR_ROUNDS = 1
OUTPUT_TAIL_CHARS = 1800
FINGERPRINT_MAX_BYTES = 5_000_000

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_UNAVAILABLE = "unavailable"
STATUS_SKIPPED = "skipped"
NON_FAILURE_STATUSES = {STATUS_PASSED, STATUS_UNAVAILABLE, STATUS_SKIPPED}

PYTEST_HINT_FILES = ("pytest.ini", "conftest.py", "tox.ini")
NPM_STUB_RE = re.compile(r"no test specified", re.IGNORECASE)
TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.py$")
UNAVAILABLE_OUTPUT_RE = re.compile(
    r"No module named ['\"]?pytest|command not found|Missing script|not recognized as an internal",
    re.IGNORECASE,
)
NO_TESTS_RE = re.compile(r"Ran 0 tests|NO TESTS RAN|no tests ran|collected 0 items", re.IGNORECASE)
PY_DEPENDENCY_FILES = {"pyproject.toml", "requirements.txt", "uv.lock", "setup.py", "setup.cfg"}

CLAIM_SUBJECT = r"(?:测试|用例|检查|校验|验证|pytest|unittest|lint)"
CLAIM_RE = re.compile(
    CLAIM_SUBJECT + r"\s*(?:全部|均|都|已经|已|也){0,2}通过"
    r"|已(?:经)?(?:验证|测试)|验证(?:无误|成功)"
    r"|(?<![A-Za-z])tests?\s+(?:are\s+|were\s+)?(?:all\s+)?pass(?:ed|es|ing)?(?![A-Za-z])"
    r"|(?<![A-Za-z])all\s+(?:the\s+)?tests?\s+(?:are\s+)?pass(?:ed|ing)?"
    r"|(?<![A-Za-z])\d+\s+passed(?![A-Za-z])"
    r"|(?<![A-Za-z])(?<!not )(?<!un)(?<!not yet )verified(?![A-Za-z])",
    re.IGNORECASE,
)
# 句子里出现这些词，说明是在提要求、问问题、表达条件或否定，不是在声称“已经验证”
HEDGE_RE = re.compile(
    r"[?？]|请|确认|等(?![价于同])|待|如果|若|需要|应该|是否|能否|才能|之前|后再|未(?!来)|尚|还没"
    r"|没有?(?:验证|运行|通过|跑|执行)|不能|无法|仍有|(?<![没无])失败"
    r"|\b(?:if|once|until|before|after|make sure|ensure|should|need|please|cannot|could not|couldn't|can't|"
    r"not|yet|fail(?:s|ed|ing)?|unable)\b",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"[。！!\n;；]|(?<=[a-z0-9])\.(?:\s|$)")
RUNTIME_LINE_RE = re.compile(r"^\s*[✓✗⚠]")
TEST_TOOL_RE = re.compile(
    r"^(?:[\w./-]*python[\d.]*\s+-m\s+(?:pytest|unittest|compileall|mypy|ruff|flake8)\b"
    r"|(?:pytest|py\.test|tox|nox|jest|vitest|mocha|ctest|mypy|tsc|eslint|ruff|flake8)\b"
    r"|cargo\s+(?:test|check|clippy)\b|go\s+(?:test|vet)\b|(?:npm|yarn|pnpm)\s+(?:run\s+)?test\b"
    r"|make\s+(?:test|check)\b)",
    re.IGNORECASE,
)
RUNNER_PREFIX_RE = re.compile(r"^(?:(?:[A-Za-z_]\w*=\S*|uv\s+run|poetry\s+run|npx|env|time)\s+)+")
COMMAND_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")  # 引号没闭合、shlex 解析不了时的回退
PYTHON_NAME_RE = re.compile(r"python[\d.]*$")
FRAMEWORK_TEST_MARKERS = ("unittest", "pytest", "TestCase", "def test", "class Test")
ASSERT_SCRIPT_RE = re.compile(r"\bassert\b|\bsys\.exit\b")
MAX_SCRIPT_TESTS = 3
PASS_SUMMARY_RE = re.compile(r"\b[1-9]\d* passed\b|^OK\b|^ok\s|test result: ok\.", re.MULTILINE)
FAIL_SUMMARY_RE = re.compile(
    r"\b[1-9]\d* (?:failed|errors?)\b|\bFAIL(?:ED)?\b|^not ok\b|--- FAIL|Traceback \(most recent", re.MULTILINE
)


@dataclass(frozen=True)
class VerifyConfig:
    mode: str = "off"
    commands: tuple = ()
    timeout: int = DEFAULT_TIMEOUT
    max_repair_rounds: int = DEFAULT_REPAIR_ROUNDS

    @property
    def enabled(self):
        return self.mode != "off"


def resolve_verify_config(values, *, mode=None, default_mode="ask"):
    """从合并后的配置字典解析 `[verify]`；mode 是命令行覆盖值。"""
    section = dict((values or {}).get("verify", {}) or {})
    resolved_mode = str(mode or section.get("mode") or default_mode)
    if resolved_mode not in VERIFY_MODES:
        raise ValueError(f"verify.mode must be one of {list(VERIFY_MODES)}")
    commands = section.get("commands", []) or []
    if isinstance(commands, str):
        commands = [commands]
    commands = tuple(str(item).strip() for item in commands if str(item).strip())
    timeout = int(section.get("timeout", DEFAULT_TIMEOUT))
    rounds = int(section.get("max_repair_rounds", DEFAULT_REPAIR_ROUNDS))
    if not 1 <= timeout <= 1800:
        raise ValueError("verify.timeout must be in [1, 1800]")
    if not 0 <= rounds <= 5:
        raise ValueError("verify.max_repair_rounds must be in [0, 5]")
    return VerifyConfig(mode=resolved_mode, commands=commands, timeout=timeout, max_repair_rounds=rounds)


def python_command(root):
    root = Path(root)
    for relative in (".venv/bin/python", "venv/bin/python"):
        if (root / relative).is_file():
            return shlex.quote("./" + relative)
    for name in ("python3", "python"):
        if which(name):
            return name
    return shlex.quote(sys.executable)


def detect_commands(root, changed_paths):
    """按本回合改动的文件推断该跑什么，便宜、针对性强的排在前面；空列表表示没什么可验证的。"""
    root = Path(root)
    paths = [str(path) for path in changed_paths if str(path).strip()]
    categories = build_artifact_graph(root, paths)["categories"]
    if not paths or all(path in categories["docs"] for path in paths):
        return []
    commands = []
    python_files = [path for path in paths if path.endswith(".py") and (root / path).is_file()]
    python_deps = [path for path in categories["dependencies"] if Path(path).name in PY_DEPENDENCY_FILES]
    if python_files or python_deps or any(TEST_FILE_RE.search(path) for path in paths):
        commands.extend(_python_commands(root, python_files))
    if (categories["frontend"] or categories["dependencies"]) and _npm_has_test_script(root):
        commands.append({"command": "npm test", "reason": "package.json 定义了 test 脚本"})
    return commands[:6]


def _python_commands(root, python_files):
    py = python_command(root)
    found = _find_test_files(root)
    if not found:
        if not python_files:
            return []
        quoted = " ".join(shlex.quote(path) for path in python_files[:20])
        return [{"command": f"{py} -m compileall -q {quoted}", "reason": "改了 Python 文件但没有测试，至少做语法检查"}]
    if _all_assert_scripts(root, found):
        return _script_commands(root, py, python_files, found)
    if _uses_pytest(root):
        commands = []
        targeted = _targeted_tests(root, python_files, found)
        if targeted and len(targeted) < len(found):
            quoted = " ".join(shlex.quote(path) for path in targeted[:20])
            commands.append({"command": f"{py} -m pytest -q -x {quoted}", "reason": "先跑与改动相关的测试，失败得更快"})
        commands.append({"command": f"{py} -m pytest -q", "reason": "项目使用 pytest"})
        return commands
    test_dirs = sorted({str(Path(path).parent) for path in found})
    discover = f"discover -q -s {shlex.quote(test_dirs[0])}" if len(test_dirs) == 1 and test_dirs[0] != "." else "-q"
    return [{"command": f"{py} -m unittest {discover}", "reason": "项目有 unittest 风格的测试"}]


def _all_assert_scripts(root, found):
    """测试文件全是不依赖框架的断言脚本（unittest/pytest 一个都收集不到）时为 True；没有 assert 的文件（含空文件）不算。"""
    for relative in found:
        try:
            text = (root / relative).read_text(encoding="utf-8", errors="replace")[:20_000]
        except OSError:
            return False
        if not ASSERT_SCRIPT_RE.search(text) or any(marker in text for marker in FRAMEWORK_TEST_MARKERS):
            return False
    return True


def _script_commands(root, py, python_files, scripts):
    targeted = _targeted_tests(root, python_files, scripts)
    ordered = targeted + [path for path in scripts if path not in targeted]
    commands = []
    for relative in ordered[:MAX_SCRIPT_TESTS]:
        prefix = "PYTHONPATH=.${PYTHONPATH:+:$PYTHONPATH} " if "/" in relative else ""  # 子目录里的脚本找不到根目录的模块
        reason = "测试文件是直接运行的断言脚本"
        if len(ordered) > MAX_SCRIPT_TESTS:
            reason += f"（共 {len(ordered)} 个，只跑前 {MAX_SCRIPT_TESTS} 个）"
        commands.append({"command": f"{prefix}{py} {shlex.quote(relative)}", "reason": reason})
    return commands


def _find_test_files(root):
    found = []
    skipped = IGNORED_PATH_NAMES | {"node_modules", "site-packages", "dist", "build"}
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in skipped)
        for name in sorted(files):
            relative = (Path(current) / name).relative_to(root).as_posix()
            if TEST_FILE_RE.search(relative):
                found.append(relative)
        if len(found) >= 500:
            break
    return found


def _targeted_tests(root, python_files, found):
    wanted = {path for path in python_files if TEST_FILE_RE.search(path)}
    stems = {Path(path).stem for path in python_files if not TEST_FILE_RE.search(path)}
    for relative in found:
        stem = Path(relative).stem
        if stem.removeprefix("test_").removesuffix("_test") in stems:
            wanted.add(relative)
    return sorted(wanted)


def _uses_pytest(root):
    if any((root / name).is_file() for name in PYTEST_HINT_FILES) or (root / "tests" / "conftest.py").is_file():
        return True
    for name in ("pyproject.toml", "setup.cfg", "requirements.txt", "requirements-dev.txt"):
        try:
            text = (root / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "pytest" in text:
            return True
    return False


def _npm_has_test_script(root):
    try:
        scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts", {}) or {}
    except (OSError, ValueError, AttributeError):
        return False
    test = str(scripts.get("test", "")) if isinstance(scripts, dict) else ""
    return bool(test) and not NPM_STUB_RE.search(test)


def changes_fingerprint(root, paths):
    """用文件内容而不是 git 状态做指纹：同一文件被再次改写也能被察觉。"""
    digest = hashlib.sha256()
    for relative in sorted(set(str(path) for path in paths)):
        digest.update(relative.encode("utf-8") + b"\0")
        target = Path(root) / relative
        try:
            if not target.is_file():
                digest.update(b"deleted")
            elif target.stat().st_size > FINGERPRINT_MAX_BYTES:
                digest.update(f"big:{target.stat().st_size}:{target.stat().st_mtime_ns}".encode())
            else:
                digest.update(hashlib.sha256(target.read_bytes()).digest())
        except OSError:
            digest.update(b"unreadable")
    return digest.hexdigest()[:16]


def run_command(agent, command, timeout):
    """执行一条验证命令并返回回执；超时或用户中止会杀掉整个进程组，避免 pytest/jest 子进程残留。"""
    started = time.monotonic()
    env = dict(agent.shell_env(), CI="1")  # CI=1 让 jest/vitest 之类不进入 watch 模式
    timed_out = aborted = False
    code = None
    try:
        if _sandbox_active(agent, command):
            done = agent.sandbox_runner.run(command, cwd=agent.root, env=env, timeout=timeout)
            code, output = done.returncode, (done.stdout or "") + (done.stderr or "")
        else:
            code, output, timed_out, aborted = _run_plain(command, agent.root, env, timeout, agent)
    except subprocess.TimeoutExpired as exc:
        timed_out, output = True, _decode(exc.stdout) + _decode(exc.stderr)
    except (OSError, RuntimeError) as exc:
        output = f"{type(exc).__name__}: {exc}"
        return _receipt(agent, command, None, STATUS_UNAVAILABLE, started, output)
    status = STATUS_SKIPPED if aborted else _classify(command, code, output, timed_out)
    return _receipt(agent, command, code, status, started, output)


def _sandbox_active(agent, command):
    """只有沙箱真的会包住命令时才走 SandboxRunner；退化成裸跑时用下面能杀进程组的路径。"""
    config = getattr(agent, "sandbox_config", None)
    runner = getattr(agent, "sandbox_runner", None)
    if config is None or runner is None or not config.enabled:
        return False
    if config.mode == "required":
        return True
    if command_is_excluded(command, config.excluded_commands):
        return False
    return bool(SandboxChecker(runner.which).backend_path(config.backend))


def _run_plain(command, cwd, env, timeout, agent):
    process = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        return _wait(process, deadline, agent)
    except BaseException:
        _kill_group(process)  # Ctrl-C 等中断：start_new_session 下 SIGINT 到不了子进程，不能留下孤儿 pytest
        raise


def _wait(process, deadline, agent):
    while True:
        try:
            output, _ = process.communicate(timeout=max(0.05, min(0.5, deadline - time.monotonic())))
            return process.returncode, output or "", False, False
        except subprocess.TimeoutExpired as exc:
            aborted = bool(getattr(agent, "abort_requested", False))
            if not aborted and time.monotonic() < deadline:
                continue
            _kill_group(process)
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                output = _decode(exc.stdout)
            return None, output or "", not aborted, aborted


def _kill_group(process):
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _classify(command, code, output, timed_out):
    if timed_out:
        return STATUS_TIMEOUT
    if NO_TESTS_RE.search(output or "") and code in (0, 5):
        return STATUS_SKIPPED  # 一个测试都没跑到，既不能算通过也不能算失败
    if code == 0:
        return STATUS_PASSED
    if code in (126, 127) or UNAVAILABLE_OUTPUT_RE.search(output or ""):
        return STATUS_UNAVAILABLE
    if code == 5 and "pytest" in command:
        return STATUS_SKIPPED
    return STATUS_FAILED


def _receipt(agent, command, code, status, started, output):
    tail = str(output or "").strip()[-OUTPUT_TAIL_CHARS:]
    return {
        "command": agent.redact_text(command),
        "status": status,
        "exit_code": code,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "output_tail": agent.redact_text(tail),
    }


def claims_verified(text):
    """回答里是否在断言“已经验证/测试通过”；提要求、问问题、带条件或否定的句子不算，运行时自己追加的 ✓/✗/⚠ 行也不算。"""
    for line in str(text or "").splitlines():
        if RUNTIME_LINE_RE.match(line):
            continue
        for sentence in SENTENCE_SPLIT_RE.split(line):
            if CLAIM_RE.search(sentence) and not HEDGE_RE.search(sentence):
                return True
    return False


def _stage_runs_test(tokens):
    name, args = stage_head(tokens)
    text = RUNNER_PREFIX_RE.sub("", " ".join([name, *args]).strip())
    if TEST_TOOL_RE.match(text):
        return True
    parts = text.split()
    if parts and PYTHON_NAME_RE.match(parts[0]):
        script = next((part for part in parts[1:] if not part.startswith("-")), "")
        return bool(TEST_FILE_RE.search(script.strip("'\"")))
    return False


def runs_test_tool(command):
    """命令里是否真的在运行测试/检查工具（而不只是提到它的名字，比如 echo pytest、pip install pytest）。"""
    segments = split_segments(command)
    if segments is None:
        parts = (RUNNER_PREFIX_RE.sub("", part.strip()) for part in COMMAND_SPLIT_RE.split(str(command or "")))
        return any(TEST_TOOL_RE.match(part) for part in parts)
    return any(_stage_runs_test(stage) for segment in segments for stage in segment.stages)


def _summary_shows_pass(content):
    text = str(content or "")
    return bool(PASS_SUMMARY_RE.search(text)) and not FAIL_SUMMARY_RE.search(text) and not NO_TESTS_RE.search(text)


def _exit_vouches_for_tests(command, content):
    """退出码 0 是否真的说明测试通过：测试命令必须是最后执行的、没被管道接走，后面只能跟 `&&`。

    `pytest | tail`、`pytest; echo done`、`pytest || true` 的退出码属于别的命令，
    这几种只在输出里有明确的"全部通过"摘要时才算。
    """
    segments = split_segments(command)
    if segments is None:
        return runs_test_tool(command)
    ran_tests = False
    for index, segment in enumerate(segments):
        if not any(_stage_runs_test(stage) for stage in segment.stages):
            continue
        ran_tests = True
        chained = all(item.separator == "&&" for item in segments[index:-1]) and not segments[-1].separator
        if _stage_runs_test(segment.stages[-1]) and chained:  # 末尾的 `&` 表示放到后台，退出码和测试无关
            return True
    return ran_tests and _summary_shows_pass(content)


def shell_evidence(history_items):
    """本回合里模型自己是否成功跑过测试/检查类命令（run_shell 且这次退出码 0 确实属于测试命令）。"""
    for item in history_items:
        if item.get("role") != "tool" or item.get("name") != "run_shell":
            continue
        content = str(item.get("content", ""))
        if re.search(r"exit_code:\s*0\b", content) and _exit_vouches_for_tests(
            (item.get("args") or {}).get("command", ""), content
        ):
            return True
    return False
