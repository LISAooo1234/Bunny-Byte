"""测试篡改守卫：验证通过之前，先看本轮有没有把测试本身改弱。

模型最省事的“修复”往往是改测试：删掉失败的测试、加上 skip、删断言、把断言包进 `try/except`、
在 pytest 配置里 `--deselect` 掉它。这样得到的“全部通过”不能当证据。这里只读本轮影子快照前后两个 tree
（`run_shell rm` / `sed -i` 也看得见），不调用模型、不运行任何东西，按 `ast` 和行做确定性的判断：

- 测试按数量算净减少（改名、在文件之间搬动会互相抵消；挪出收集范围的文件算删除），连同被测源码一起删掉的只提示；
- 断言净减少（扣掉被删测试里的断言，挪进同文件辅助函数的照样算数；本轮加了 parametrize / subTest 的不算），
  或已有文件里新增 `assert True`、吞掉 AssertionError / Exception 的 `try`；
- 已有测试文件、conftest.py 里新增 skip / xfail / importorskip（按平台、版本跳过的只提示）；
- pytest 收集配置新增排除（`addopts` 的 `--deselect` / `--ignore` / `-k`、`norecursedirs`、缩小 `testpaths`、
  conftest 的 `collect_ignore` / `pytest_collection_modifyitems`）。

本轮新建的测试文件不查 skip 和断言；只改了期望值（`== 3` → `== 2`）不算削弱，只作为提示列出测试名。
"""

from __future__ import annotations

import ast
import configparser
import difflib
import re
import shlex
from collections import Counter
from functools import lru_cache
from dataclasses import replace
from pathlib import PurePosixPath

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from .fix_proof import is_test_path
from .shadow_snapshots import _LOCK, ShadowError, ShadowStore
from .turn_diff import current_turn_entry, text_or_none
from .verification import TEST_FILE_RE

CLEAN, SUSPICIOUS, SKIPPED = "clean", "suspicious", "skipped"
WARN, INFO = "warn", "info"
MODE_RANK = {"off": 0, "": 1, "warn": 1, "block": 2}
MAX_FILES = 60
MAX_DIFF_LINES = 4000
MAX_FUZZY = 20_000  # 改名配对的相似度比较次数上限（几百个测试同时改名时不在回合末尾卡十几秒）
PAIR_RATIO = 0.6  # 消失的测试和新出现的测试，函数体至少这么像才算改名 / 搬家
MAX_NAMES = 5
INI_FILES = {"pytest.ini", "tox.ini", "setup.cfg"}
CONFTEST = "conftest.py"
SELECTOR_FLAGS = ("--deselect", "--ignore-glob", "--ignore", "-k")
CONFTEST_HOOK_RE = re.compile(r"\bpytest_collection_modifyitems\b|\bpytest_ignore_collect\b|\bpytest_deselected\b")
COLLECT_IGNORE_RE = re.compile(r"\bcollect_ignore(?:_glob)?\b")
SKIP_RE = re.compile(
    r"\bpytest\.mark\.(?:skip(?:if)?|xfail)\b|\bpytest\.(?:skip|xfail|importorskip)\s*\(|\bunittest\.(?:skip\w*|expectedFailure)\b"
    r"|^\s*@(?:skip(?:If|Unless)?|expectedFailure)\b|\.skipTest\s*\(|\braise\s+(?:unittest\.)?SkipTest\b"
)
# 按平台 / 解释器版本 / 系统命令是否存在跳过：通常是正当的，只提示
ENV_SKIP_RE = re.compile(r"sys\.platform|sys\.version|platform\.|os\.name|shutil\.which|find_spec|\bwin32\b|\bdarwin\b")
ASSERT_LINE_RE = re.compile(r"^\s*assert\b|\.assert\w*\s*\(|\bpytest\.raises\s*\(|\b(?:self|pytest)\.fail\s*\(")
NEUTRAL_LINE_RE = re.compile(r"^\s*assert\s+(?:True|1)\s*(?:,.*)?$|^\s*except\s*(?::|\(?[\w.,\s]*\b(?:AssertionError|Exception|BaseException)\b)")
TEST_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test\w*)\s*\(", re.MULTILINE)
EXPANDED_RE = re.compile(r"\bparametrize\b|\bsubTest\b")
SWALLOWING = {"AssertionError", "Exception", "BaseException"}

LABELS = {
    "removed_tests": "删除了测试",
    "obsolete_tests": "随源码一起删除的测试",
    "assertions_removed": "删除了断言",
    "assertions_neutralised": "断言被架空",
    "skip_added": "新增跳过标记",
    "env_skip_added": "新增按环境跳过",
    "collection_narrowed": "测试收集范围被缩小",
    "config_changed": "改了测试收集配置",
    "expectations_changed": "改了已有测试的断言",
}


def tamper_mode(config):
    """`[verify] tamper` 的实际取值；没写时是 warn。"""
    return str(getattr(config, "tamper", "") or "warn")


def stricter_tamper(config, trusted):
    """仓库自带的配置只能把守卫调严：比用户自己的全局配置松（或同级但不同写法）时退回全局的值。"""
    value, trusted = str(config.tamper or ""), str(trusted or "")
    if value == trusted or MODE_RANK.get(value, 1) > MODE_RANK.get(trusted, 1):
        return config
    return replace(config, tamper=trusted)


def require_block(config):
    """`bunny -p --require-verified`：没人明确设置时，篡改直接让结果不算已验证。"""
    return replace(config, tamper="block") if not getattr(config, "tamper", "") else config


def _is_test_file(path):
    return path.endswith(".py") and PurePosixPath(path).name != CONFTEST and is_test_path(path)


def _is_config_file(path):
    return PurePosixPath(path).name in INI_FILES | {"pyproject.toml", CONFTEST}


def relevant_paths(paths):
    """本轮改过的、守卫要看的文件：测试 .py 和 pytest 收集配置；一个都没有就不必读快照。"""
    found = []
    for path in paths or []:
        normalized = str(path).replace("\\", "/")
        if _is_test_file(normalized) or _is_config_file(normalized):
            found.append(str(path))
    return found


def _collected(path):
    """pytest 默认的 python_files（test_*.py / *_test.py）和 unittest 的 test*.py 里的测试才会被收集。"""
    return bool(TEST_FILE_RE.search(path)) or PurePosixPath(path).name.startswith("test")


def _tree(text):
    try:
        return ast.parse(text or "")
    except (SyntaxError, ValueError):
        return None


def _call_name(node):
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else "")


def _is_assertion(node):
    if isinstance(node, ast.Assert):
        return True
    return isinstance(node, ast.Call) and (_call_name(node).startswith("assert") or _call_name(node) in {"raises", "fail"})


def _assertions(node):
    return [child for child in ast.walk(node) if _is_assertion(child)]


def _handler_swallows(handler):
    """except 接住了断言失败、而且处理里没有再抛出 / fail / 断言：失败被吞掉了。"""
    if any(isinstance(node, ast.Raise) or _is_assertion(node) for stmt in handler.body for node in ast.walk(stmt)):
        return False
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any((item.attr if isinstance(item, ast.Attribute) else getattr(item, "id", "")) in SWALLOWING for item in names)


def _checks(nodes):
    """(断言里比较的每一项的源码, 不含比较的断言个数, 有没有 `or`)。

    把几条断言合成 `assert a == 1 and b == 2` 时比较项都还在；`or` 会让合并后的断言变松，不算合并。
    """
    found, bare, loose = set(), 0, False
    for node in nodes:
        compares = [child for child in ast.walk(node) if isinstance(child, ast.Compare)]
        if not compares and not (isinstance(node, ast.Call) and _call_name(node) in {"assertEqual", "assertEquals", "assertIn", "assertIs"}):
            bare += 1
        if isinstance(node, ast.Call) and _call_name(node) in {"assertEqual", "assertEquals"} and len(node.args) >= 2:
            found.add(f"{_unparse(node.args[0])} == {_unparse(node.args[1])}")
        loose = loose or any(isinstance(child, ast.BoolOp) and isinstance(child.op, ast.Or) for child in ast.walk(node))
        found |= {_unparse(child) for child in compares}
    return found, bare, loose


def _neutral(module):
    """`assert True` / `self.assertTrue(True)` 和把断言包起来吞掉失败的 try 的个数。"""
    count = 0
    for node in ast.walk(module):
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant) and node.test.value:
            count += 1
        elif isinstance(node, ast.Call) and _call_name(node) == "assertTrue" and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value:
            count += 1
        elif isinstance(node, ast.Try) and any(_handler_swallows(h) for h in node.handlers) and any(_assertions(stmt) for stmt in node.body):
            count += 1
    return count


class _Facts:
    """一个测试文件里守卫关心的数字：测试名（可重复）、每个测试的断言、断言总数、被架空的断言数。

    断言总数按测试计：测试里直接写的，加上它调用的同文件辅助函数里的（挪进 `check(...)` 不算删断言）。
    """

    def __init__(self, text):
        self.text = text or ""
        module = _tree(self.text)
        self.parsed = module is not None
        self.tests = []  # [(函数名, 断言数, {断言源码}, 函数体源码)]
        self.checks = {}  # 函数名 -> 断言里的比较项
        if module is None:
            self.tests = [(name, 0, frozenset(), "") for name in TEST_DEF_RE.findall(self.text)]
            self.asserts = sum(1 for line in self.text.splitlines() if _code(line) and ASSERT_LINE_RE.search(line))
            self.neutral = sum(1 for line in self.text.splitlines() if NEUTRAL_LINE_RE.search(line))
            return
        functions = [node for node in ast.walk(module) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        helpers = {node.name: len(_assertions(node)) for node in functions if not node.name.startswith("test")}
        tests = [node for node in functions if node.name.startswith("test")]
        for node in tests:
            found = _assertions(node)
            called = sum(helpers.get(_call_name(call), 0) for call in ast.walk(node) if isinstance(call, ast.Call))
            body = "\n".join(_unparse(stmt) for stmt in node.body)
            self.tests.append((node.name, len(found) + called, frozenset(_unparse(item) for item in found), body))
            self.checks[node.name] = _checks(found)
        self.asserts = sum(item[1] for item in self.tests)
        self.neutral = _neutral(module)


def _unparse(node):
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - 理论上不会发生
        return ast.dump(node)


def _code(line):
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


@lru_cache(maxsize=128)
def _added_removed(old, new):
    """(新增行 [(新文件里的行号, 文本)], 删除行文本)。difflib 是平方级的：大文件改用按行计数的多重集合比较。"""
    old_lines, new_lines = (old or "").splitlines(), (new or "").splitlines()
    added, removed = [], []
    if len(old_lines) + len(new_lines) > MAX_DIFF_LINES:
        left = Counter(old_lines)
        for index, line in enumerate(new_lines):
            if left[line] > 0:
                left[line] -= 1
            else:
                added.append((index, line))
        removed = list((Counter(old_lines) - Counter(new_lines)).elements())
        return tuple(added), tuple(removed)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed.extend(old_lines[i1:i2])
        if tag in ("replace", "insert"):
            added.extend((index, new_lines[index]) for index in range(j1, j2))
    return tuple(added), tuple(removed)


def _finding(kind, severity, paths=(), detail="", names=()):
    return {
        "kind": kind,
        "severity": severity,
        "label": LABELS[kind],
        "paths": sorted(set(paths))[:MAX_NAMES],
        "detail": detail,
        "names": list(dict.fromkeys(names))[:MAX_NAMES],
    }


def _decorated_test(lines, index):
    """新增的 skip 行属于哪个测试：往下找被装饰的 def，找不到就往上找所在的 def。"""
    for line in lines[index : index + 6]:
        match = TEST_DEF_RE.match(line)
        if match:
            return match.group(1)
    for line in reversed(lines[: index + 1]):
        match = re.match(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(", line)
        if match:
            return match.group(1)
    return ""


def _env_condition(line):
    """只有 skipif / importorskip 才可能是按环境跳过，而且看的是条件本身，不看 reason 文字。"""
    if "importorskip" in line:
        return True
    match = re.search(r"\bskip(?:if|If|Unless)\s*\((.*)", line)
    if not match:
        return False
    condition = re.split(r",\s*(?:reason\s*=|['\"])", match.group(1), maxsplit=1)[0]
    return bool(ENV_SKIP_RE.search(condition))


def _skip_marks(text):
    """{测试名: [skip 行]}：每个测试前后的 skip / xfail 标记（按测试比较，换掉一个条件跳过不会被别处的删除抵消）。"""
    lines = (text or "").splitlines()
    marks = {}
    for index, line in enumerate(lines):
        if _code(line) and SKIP_RE.search(line):
            marks.setdefault(_decorated_test(lines, index) or "<module>", []).append(line.strip())
    return marks


def _skips(path, status, old, new, findings):
    if status == "A" and PurePosixPath(path).name != CONFTEST:
        return  # 新写的测试文件里自带 skip 是作者的选择，不是削弱已有测试
    before, after = _skip_marks(old), _skip_marks(new)
    hard, soft = [], []
    for test, lines in after.items():
        previous = list(before.get(test, []))
        for line in lines:
            if line in previous:
                previous.remove(line)
                continue
            if _env_condition(line):
                soft.append(test)
            elif not any(not _env_condition(item) for item in previous):  # 已有的无条件跳过改写法不算新增
                hard.append(test)
    if hard:
        findings.append(_finding("skip_added", WARN, [path], f"{len(hard)} 处", hard))
    if soft:
        findings.append(_finding("env_skip_added", INFO, [path], f"{len(soft)} 处", soft))


def _ini_options(path, text):
    """{选项: 值}，只看 pytest 自己的配置段。"""
    if not text:
        return {}
    name = PurePosixPath(path).name
    if name == "pyproject.toml":
        try:
            section = tomllib.loads(text).get("tool", {}).get("pytest", {}).get("ini_options", {})
        except (tomllib.TOMLDecodeError, AttributeError):
            return {}
        return {key: " ".join(map(str, value)) if isinstance(value, list) else str(value) for key, value in dict(section).items()}
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return {}
    for section in ("pytest", "tool:pytest"):
        if parser.has_section(section) and (name != "setup.cfg" or section == "tool:pytest"):
            return dict(parser.items(section))
    return {}


def _words(value):
    try:
        return shlex.split(str(value or ""))
    except ValueError:
        return str(value or "").split()


def _selectors(value):
    """addopts 里会排除测试的选项（连同它的值）。"""
    words, found = _words(value), []
    for index, word in enumerate(words):
        flag = next((item for item in SELECTOR_FLAGS if word == item or word.startswith(item + "=")), None)
        if flag:
            found.append(word if "=" in word or index + 1 >= len(words) else f"{word} {words[index + 1]}")
    return found


def _hook_only_marks(text, pattern):
    """新增的 pytest_collection_modifyitems 只给测试加标记（add_marker），没有删 items / deselect：不算缩小范围。"""
    if pattern is not CONFTEST_HOOK_RE:
        return False
    module = _tree(text)
    if module is None:
        return False
    hooks = [node for node in ast.walk(module) if isinstance(node, ast.FunctionDef) and CONFTEST_HOOK_RE.search(node.name)]
    if not hooks or any(node.name != "pytest_collection_modifyitems" for node in hooks):
        return False
    for hook in hooks:
        for node in ast.walk(hook):
            if isinstance(node, ast.Call) and _call_name(node) not in {"add_marker", "get_closest_marker", "iter_markers", "getoption", "startswith", "endswith", "get"}:
                return False
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)) and not all(isinstance(t, ast.Name) for t in getattr(node, "targets", [getattr(node, "target", None)])):
                return False
            if isinstance(node, (ast.Assign, ast.AugAssign)) and any(isinstance(t, ast.Name) and t.id == "items" for t in getattr(node, "targets", [getattr(node, "target", None)])):
                return False
    return True


def _config(path, status, old, new, findings):
    name = PurePosixPath(path).name
    if name == CONFTEST:
        added, removed = _added_removed(old, new)
        for pattern in (CONFTEST_HOOK_RE, COLLECT_IGNORE_RE):
            new_hits = [line.strip() for _, line in added if _code(line) and pattern.search(line)]
            if len(new_hits) > sum(1 for line in removed if _code(line) and pattern.search(line)) and not _hook_only_marks(new, pattern):
                findings.append(_finding("collection_narrowed", WARN, [path], new_hits[0][:80]))
        return
    before, after = _ini_options(path, old), _ini_options(path, new)
    if before == after:
        return
    narrowed = [item for item in _selectors(after.get("addopts")) if item not in _selectors(before.get("addopts"))]
    added_dirs = set(_words(after.get("norecursedirs"))) - set(_words(before.get("norecursedirs")))
    narrowed += [f"norecursedirs {item}" for item in sorted(added_dirs) if before or after.get("norecursedirs")]
    old_paths, new_paths = set(_words(before.get("testpaths"))), set(_words(after.get("testpaths")))
    if old_paths and new_paths and old_paths - new_paths:
        narrowed.append(f"testpaths 去掉了 {'、'.join(sorted(old_paths - new_paths))}")
    if narrowed:
        findings.append(_finding("collection_narrowed", WARN, [path], "；".join(narrowed)[:160]))
        return
    changed = sorted(key for key in set(before) | set(after) if key in {"testpaths", "python_files", "python_classes", "python_functions", "norecursedirs"} and before.get(key) != after.get(key))
    if changed:
        findings.append(_finding("config_changed", INFO, [path], "、".join(changed)))


def _removed_source_defs(changes):
    """本轮从源码（非测试 .py）里删掉的函数 / 类名和整个删掉的模块名：用来认出“随源码一起删的旧测试”。"""
    names, modules = set(), set()

    def defs(module):
        return {n.name for n in ast.walk(module) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))} if module else set()

    for status, path, old, new in changes:
        if not path.endswith(".py") or is_test_path(path):
            continue
        if status == "D":
            modules.add(PurePosixPath(path).stem)
        names |= defs(_tree(old)) - defs(_tree(new))
    return names, modules


def _is_obsolete(test, names, modules):
    """测试文件连同被测模块一起删掉，或测试名正好是被删的函数名（test_old_api ↔ old_api），或测试体调用了被删的函数。"""
    stem = PurePosixPath(test["path"]).stem
    module = stem[5:] if stem.startswith("test_") else stem.removesuffix("_test")
    if test["status"] == "D" and module in modules:
        return True
    bare = test["name"].removeprefix("test_").removeprefix("test")
    if bare in names:
        return True
    called = set(re.findall(r"\b(\w+)\s*\(", test["body"]))
    return bool(called & {name for name in names if len(name) >= 3})


def _name_words(name):
    return {word for word in re.split(r"_+|(?<=[a-z])(?=[A-Z])", name.lower()) if word and word != "test"}


def _renamed(old, new):
    """同一个测试换了名字 / 挪了地方：函数体一样，或者名字有共同的词、函数体也大体一样。

    短测试的函数体都很像（`assert add(0, 0) == 0` 和 `assert add(1, 1) == 2`），只看相似度会让新写的不相干测试抵掉被删的测试。
    """
    if old["body"] == new["body"]:
        return True
    if not (old["body"] and new["body"] and _name_words(old["name"]) & _name_words(new["name"])):
        return False
    matcher = difflib.SequenceMatcher(None, old["body"], new["body"], autojunk=False)
    return matcher.quick_ratio() >= PAIR_RATIO and matcher.ratio() >= PAIR_RATIO


def _unpaired(gone, new):
    """(消失的测试里配不上对的, 改了名而且函数体也变了的测试名)。新写的不相干测试抵不掉被删的测试。

    先按同名、再按完全相同的函数体配对（哈希查找）；剩下的做相似度比较，最多比 MAX_FUZZY 对，超出的按配不上算。
    """
    pool = list(new)
    by_name, by_body = {}, {}
    for index, item in enumerate(pool):
        by_name.setdefault(item["name"], []).append(index)
        by_body.setdefault(item["body"], []).append(index)
    used, left, rewritten = set(), [], []
    pending = []
    for test in gone:
        index = next((i for i in by_name.get(test["name"], []) + by_body.get(test["body"], []) if i not in used), None)
        if index is None:
            pending.append(test)
        else:
            used.add(index)
            if pool[index]["body"] != test["body"]:
                rewritten.append(pool[index]["name"])
    budget = MAX_FUZZY
    for test in pending:
        index = None
        for i, item in enumerate(pool):
            if i in used:
                continue
            if budget <= 0:
                break
            budget -= 1
            if _renamed(test, item):
                index = i
                break
        if index is None:
            left.append(test)
        else:
            used.add(index)
            rewritten.append(f"{test['name']} → {pool[index]['name']}")
    return left, rewritten


def _consolidated(old_facts, new_facts):
    """同一个测试断言变少、但原来比较的每一项都还在新断言里（合并成一条 and / 一条 assertEqual 之类）：不算删断言。"""
    old_counts = {item[0]: item[1] for item in old_facts.tests}
    total = 0
    for name, count, _, _ in new_facts.tests:
        before = old_counts.get(name)
        if before is None or count >= before or name not in old_facts.checks or name not in new_facts.checks:
            continue
        (old_checks, old_bare, _), (new_checks, new_bare, loose) = old_facts.checks[name], new_facts.checks[name]
        if old_checks and old_checks <= new_checks and new_bare >= old_bare and not loose:
            total += before - count
    return total


def _unmatched(old_all, new_all):
    """(按 (文件, 名字) 原样对不上的旧测试, 对不上的新测试)。"""
    old_keys = Counter((item["path"], item["name"]) for item in old_all)
    new_keys = Counter((item["path"], item["name"]) for item in new_all)
    gone_left, new_left = old_keys - new_keys, new_keys - old_keys
    gone, fresh = [], []
    for item in old_all:
        key = (item["path"], item["name"])
        if gone_left[key] > 0:
            gone_left[key] -= 1
            gone.append(item)
    for item in new_all:
        key = (item["path"], item["name"])
        if new_left[key] > 0:
            new_left[key] -= 1
            fresh.append(item)
    return gone, fresh


def _tests(changes, findings):
    tests = [(status, path, old, new) for status, path, old, new in changes if _is_test_file(path)]
    if not tests:
        return
    old_all, new_all = [], []
    old_asserts = new_asserts = 0
    expanded = False
    neutral_paths, changed_expectations = [], []
    consolidated = 0
    for status, path, old, new in tests:
        old_facts, new_facts = _Facts(old), _Facts(new)
        old_asserts += old_facts.asserts
        new_asserts += new_facts.asserts
        if _collected(path):
            old_all += [{"name": n, "asserts": c, "body": body, "path": path, "status": status} for n, c, _, body in old_facts.tests]
            new_all += [{"name": n, "asserts": c, "body": body, "path": path} for n, c, _, body in new_facts.tests]
        added, _ = _added_removed(old, new)
        expanded = expanded or any(EXPANDED_RE.search(line) for _, line in added)
        if status != "A" and new_facts.neutral > old_facts.neutral:
            neutral_paths.append(path)
        consolidated += _consolidated(old_facts, new_facts)
        if status == "M" and old_facts.parsed and new_facts.parsed:
            old_by_name = {item[0]: item[2] for item in old_facts.tests}
            changed_expectations += [item[0] for item in new_facts.tests if old_by_name.get(item[0]) and item[2] != old_by_name[item[0]]]
    # 同名同处的测试原样留着的先抵掉；剩下消失的再找函数体相似的新测试
    gone, unmatched_new = _unmatched(old_all, new_all)
    removed, rewritten = _unpaired(gone, unmatched_new)
    changed_expectations += rewritten
    if removed:
        names, modules = _removed_source_defs(changes)
        obsolete = [item for item in removed if _is_obsolete(item, names, modules)]
        real = [item for item in removed if item not in obsolete]
        if real:
            findings.append(_finding("removed_tests", WARN, [item["path"] for item in real], f"少了 {len(real)} 个", [item["name"] for item in real]))
        if obsolete:
            findings.append(_finding("obsolete_tests", INFO, [item["path"] for item in obsolete], f"{len(obsolete)} 个", [item["name"] for item in obsolete]))
    lost = old_asserts - new_asserts - sum(item["asserts"] for item in removed) - consolidated
    if lost > 0 and not expanded:
        findings.append(_finding("assertions_removed", WARN, [path for status, path, *_ in tests if status != "A"], f"净少 {lost} 处"))
    if neutral_paths:
        findings.append(_finding("assertions_neutralised", WARN, neutral_paths, "新增 assert True 或吞掉断言失败的 try/except"))
    if changed_expectations:
        findings.append(_finding("expectations_changed", INFO, [], f"{len(changed_expectations)} 个测试", changed_expectations))


def scan_changes(changes):
    """changes：[(状态 A/M/D, 路径, 旧内容 bytes|None, 新内容 bytes|None)]。返回 {status, findings}。"""
    decoded = [(status, path, text_or_none(old), text_or_none(new)) for status, path, old, new in changes]
    findings = []
    _tests(decoded, findings)
    for status, path, old, new in decoded:
        if _is_test_file(path) or PurePosixPath(path).name == CONFTEST:
            _skips(path, status, old, new, findings)
        if _is_config_file(path):
            _config(path, status, old, new, findings)
    warn = [item for item in findings if item["severity"] == WARN]
    return {"status": SUSPICIOUS if warn else CLEAN, "findings": findings}


class _TooMany(Exception):
    pass


def _turn_changes(store, before, after):
    """只读守卫要看的文件（加上删改过的源码 .py，用来认出过时测试）的内容；大 lockfile 不读。"""
    changes = store.diff(before, after)
    relevant = [item for item in changes if relevant_paths([item["path"]])]
    if len(relevant) > MAX_FILES:
        raise _TooMany(len(relevant))
    sources = [item for item in changes if item not in relevant and item["path"].endswith(".py") and item["status"] in "DM"]
    wanted = relevant + sources[: MAX_FILES - len(relevant)]
    oids = [item[key] for item in wanted for key in ("old_oid", "oid") if set(item[key]) != {"0"}]
    with _LOCK:
        blobs = store._read_blobs(oids) if oids else {}
    return [
        (item["status"], item["path"], None if item["status"] == "A" else blobs.get(item["old_oid"]), None if item["status"] == "D" else blobs.get(item["oid"]))
        for item in sorted(wanted, key=lambda entry: entry["path"])
    ]


def scan_turn(agent, task_state):
    """本轮（主 agent）的篡改检查；没有测试 / 配置改动时返回 None，不碰快照。"""
    if not relevant_paths(task_state.changed_paths):
        return None
    entry = current_turn_entry(agent)
    store = ShadowStore.for_root(agent.root)
    if entry is None or not store.available:
        return {"status": SKIPPED, "reason": "no_snapshot", "findings": []}
    after = store.snapshot() or entry.get("after_tree")  # 子 agent 的写入不在主 agent 的快照里，现在重新拍一次
    try:
        return scan_changes(_turn_changes(store, entry.get("before_tree"), after))
    except _TooMany as exc:
        return {"status": SKIPPED, "reason": f"本轮改了 {exc.args[0]} 个测试 / 配置文件（上限 {MAX_FILES}），没有检查", "findings": []}
    except (ShadowError, OSError, ValueError, KeyError) as exc:
        return {"status": SKIPPED, "reason": f"{type(exc).__name__}: {exc}"[:200], "findings": []}


def describe(finding):
    names = "、".join(finding.get("names") or [])
    paths = "、".join(finding.get("paths") or [])
    parts = [part for part in (finding.get("detail"), names or paths) if part]
    return finding["label"] + (f"（{'：'.join(parts)}）" if parts else "")


def tamper_notice(result, mode):
    """回合末尾的一行；没有可疑发现时只在有提示级发现时说一句。"""
    findings = list((result or {}).get("findings") or [])
    warn = [item for item in findings if item["severity"] == WARN]
    if warn:
        tail = "，不算已验证" if mode == "block" else "，请确认是你要求的改动"
        return "⚠ 测试可能被削弱：" + "；".join(describe(item) for item in warn[:3]) + (f" 等 {len(warn)} 项" if len(warn) > 3 else "") + tail
    info = [item for item in findings if item["kind"] in {"obsolete_tests", "env_skip_added"}]
    return "· 测试改动：" + "；".join(describe(item) for item in info[:3]) if info else ""


def tamper_summary(result):
    """给 `bunny -p` JSON 用的一小段。"""
    if not result:
        return {}
    return {
        "status": result.get("status", ""),
        "findings": [{key: item.get(key) for key in ("kind", "severity", "paths", "names", "detail")} for item in result.get("findings") or []],
    }


def tampered(state):
    return ((state or {}).get("tamper") or {}).get("status") == SUSPICIOUS
