"""bench 任务集（七，单独的 goal 套件）：一轮 12 步做不完的任务，用来比较目标模式和单轮。

三道题都要改六到八个文件，而 patch_file 要求先读过文件，所以高效完成也要 14–18 步。
每题带一条 `goal_check`（仓库里能看见的测试），提示词里写明它，三种跑法看到的是同一句话；
隐藏 verifier 比 goal_check 更严，用来发现“检查通过、其实没做对”的假达成。
默认 `bunny bench` 不跑这一套，用 `--suite goal` 选择。
"""

from .tasks import PY, BenchTask, final, tool

CHECK = f"{PY} -m unittest -q"
CHECK_TEXT = "python3 -m unittest -q"

# ---- 题一：按 docstring 实现六个函数 -------------------------------------------------

TOOLKIT = (
    (
        "chunks",
        "chunk(items, size)",
        "Split items into lists of at most size elements, in order.\n\n    Raise ValueError if size < 1.",
        "    if size < 1:\n        raise ValueError(\"size must be >= 1\")\n    return [list(items[i:i + size]) for i in range(0, len(items), size)]",
        ("self.assertEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])", "self.assertEqual(chunk([], 3), [])"),
        ("assert chunk([1, 2, 3], 3) == [[1, 2, 3]]", "assert chunk((1, 2), 5) == [[1, 2]]", "_raises(lambda: chunk([1], -1), ValueError)"),
    ),
    (
        "clamp",
        "clamp(value, low, high)",
        "Return value limited to the closed range [low, high].\n\n    Raise ValueError if low > high.",
        "    if low > high:\n        raise ValueError(\"low > high\")\n    return max(low, min(high, value))",
        ("self.assertEqual(clamp(5, 0, 3), 3)", "self.assertEqual(clamp(-1, 0, 3), 0)"),
        ("assert clamp(2, 0, 3) == 2", "assert clamp(1.5, 1, 1) == 1", "_raises(lambda: clamp(1, 3, 0), ValueError)"),
    ),
    (
        "leap",
        "is_leap(year)",
        "Return True for Gregorian leap years: divisible by 4, except centuries,\n    except centuries divisible by 400.",
        "    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)",
        ("self.assertTrue(is_leap(2024))", "self.assertFalse(is_leap(2023))"),
        ("assert is_leap(2000) is True", "assert is_leap(1900) is False", "assert is_leap(2100) is False"),
    ),
    (
        "invert",
        "invert(mapping)",
        "Return a dict mapping each value to the sorted list of keys that had it.",
        "    result = {}\n    for key, value in mapping.items():\n        result.setdefault(value, []).append(key)\n    return {value: sorted(keys) for value, keys in result.items()}",
        ("self.assertEqual(invert({'a': 1, 'b': 2}), {1: ['a'], 2: ['b']})",),
        ("assert invert({'b': 1, 'a': 1}) == {1: ['a', 'b']}", "assert invert({}) == {}"),
    ),
    (
        "initials",
        "initials(name)",
        "Return the upper-case first letter of every whitespace-separated word,\n    each followed by a dot. Empty or blank input returns \"\".",
        "    return \"\".join(word[0].upper() + \".\" for word in name.split())",
        ("self.assertEqual(initials('ada lovelace'), 'A.L.')",),
        ("assert initials('  grace   brewster hopper ') == 'G.B.H.'", "assert initials('') == ''", "assert initials('   ') == ''"),
    ),
    (
        "rle",
        "run_length(text)",
        "Run-length encode text: every run of the same character becomes the\n    character followed by the run length, e.g. \"aaab\" -> \"a3b1\".",
        "    out = []\n    for char in text:\n        if out and out[-1][0] == char:\n            out[-1][1] += 1\n        else:\n            out.append([char, 1])\n    return \"\".join(f\"{char}{count}\" for char, count in out)",
        ("self.assertEqual(run_length('aaab'), 'a3b1')",),
        ("assert run_length('') == ''", "assert run_length('abba') == 'a1b2a1'", "assert run_length('zzzzzzzzzzzz') == 'z12'"),
    ),
)


def _name(signature):
    return signature.split("(")[0]


def _toolkit_files():
    files = {"README.md": "# toolkit\n\n六个小工具函数，每个模块一个，规格写在 docstring 里。\n"}
    for module, signature, doc, _body, visible, _hidden in TOOLKIT:
        files[f"{module}.py"] = f'def {signature}:\n    """{doc}\n    """\n    raise NotImplementedError\n'
        cases = "\n".join(f"        {line}" for line in visible)
        files[f"test_{module}.py"] = (
            f"import unittest\n\nfrom {module} import {_name(signature)}\n\n\n"
            f"class {module.capitalize()}Test(unittest.TestCase):\n    def test_basic(self):\n{cases}\n"
        )
    return files


def _toolkit_verifier():
    lines = [
        "def _raises(call, error):",
        "    try:",
        "        call()",
        "    except error:",
        "        return",
        "    raise AssertionError(f'expected {error.__name__}')",
        "",
    ]
    for module, signature, _doc, _body, _visible, hidden in TOOLKIT:
        lines.append(f"from {module} import {_name(signature)}  # noqa: E402")
    lines.append("")
    for *_rest, hidden in TOOLKIT:
        lines.extend(hidden)
    return "\n".join(lines) + "\n"


def _toolkit_solution():
    steps = [tool("run_shell", command=f"{CHECK} 2>&1 | tail -5")]
    for module, *_rest in TOOLKIT:
        steps.append(tool("read_file", path=f"{module}.py"))
    for module, _signature, _doc, body, _visible, _hidden in TOOLKIT:
        steps.append(tool("patch_file", path=f"{module}.py", old_text="    raise NotImplementedError", new_text=body))
    steps.append(tool("run_shell", command=f"{CHECK} 2>&1 | tail -3"))
    steps.append(final("六个函数都按 docstring 实现了，测试通过。"))
    return tuple(steps)


# ---- 题二：六个模块里各有一个 bug --------------------------------------------------

BUGS = (
    (
        "average",
        "def average(values):\n    \"\"\"Mean of values; 0.0 for an empty list.\"\"\"\n    if not values:\n        return 0.0\n    return sum(values) // len(values)\n",
        "sum(values) // len(values)",
        "sum(values) / len(values)",
        "from average import average\n\n\nclass AverageTest(unittest.TestCase):\n    def test_fraction(self):\n        self.assertAlmostEqual(average([1, 2]), 1.5)\n",
        "from average import average\nassert average([]) == 0.0\nassert average([1, 2, 4]) == 7 / 3\n",
    ),
    (
        "window",
        "def last_n(items, n):\n    \"\"\"The last n items (all of them if there are fewer); n == 0 gives [].\"\"\"\n    return items[-n:]\n",
        "    return items[-n:]",
        "    return items[-n:] if n > 0 else []",
        "from window import last_n\n\n\nclass WindowTest(unittest.TestCase):\n    def test_zero(self):\n        self.assertEqual(last_n([1, 2, 3], 0), [])\n",
        "from window import last_n\nassert last_n([1, 2, 3], 2) == [2, 3]\nassert last_n([1], 5) == [1]\nassert last_n([1, 2], 0) == []\n",
    ),
    (
        "tags",
        "def add_tag(tag, tags=[]):\n    \"\"\"Return a new list: tags plus tag (lower-cased, stripped). Never mutate the caller's list.\"\"\"\n    tags.append(tag.strip().lower())\n    return tags\n",
        "def add_tag(tag, tags=[]):\n    \"\"\"Return a new list: tags plus tag (lower-cased, stripped). Never mutate the caller's list.\"\"\"\n    tags.append(tag.strip().lower())\n    return tags",
        "def add_tag(tag, tags=None):\n    \"\"\"Return a new list: tags plus tag (lower-cased, stripped). Never mutate the caller's list.\"\"\"\n    return [*(tags or []), tag.strip().lower()]",
        "from tags import add_tag\n\n\nclass TagsTest(unittest.TestCase):\n    def test_independent_calls(self):\n        add_tag('a')\n        self.assertEqual(add_tag(' B '), ['b'])\n",
        "from tags import add_tag\nmine = ['x']\nassert add_tag('Y', mine) == ['x', 'y']\nassert mine == ['x']\nassert add_tag('z') == ['z']\n",
    ),
    (
        "durations",
        "def to_seconds(text):\n    \"\"\"Parse \"MM:SS\" or \"HH:MM:SS\" into seconds.\"\"\"\n    parts = [int(part) for part in text.split(\":\")]\n    minutes, seconds = parts[-2], parts[-1]\n    return minutes * 60 + seconds\n",
        "    minutes, seconds = parts[-2], parts[-1]\n    return minutes * 60 + seconds",
        "    total = 0\n    for part in parts:\n        total = total * 60 + part\n    return total",
        "from durations import to_seconds\n\n\nclass DurationsTest(unittest.TestCase):\n    def test_hours(self):\n        self.assertEqual(to_seconds('1:00:05'), 3605)\n",
        "from durations import to_seconds\nassert to_seconds('02:30') == 150\nassert to_seconds('10:00:00') == 36000\n",
    ),
    (
        "names",
        "def sort_names(names):\n    \"\"\"Sort names case-insensitively; ties keep their original order.\"\"\"\n    return sorted(names)\n",
        "    return sorted(names)",
        "    return sorted(names, key=str.lower)",
        "from names import sort_names\n\n\nclass NamesTest(unittest.TestCase):\n    def test_case(self):\n        self.assertEqual(sort_names(['bob', 'Alice']), ['Alice', 'bob'])\n",
        "from names import sort_names\nassert sort_names(['b', 'B', 'a']) == ['a', 'b', 'B']\nassert sort_names([]) == []\n",
    ),
    (
        "percent",
        "def percent(part, whole):\n    \"\"\"part as a percentage of whole, rounded to one decimal, e.g. \"33.3%\"; \"0.0%\" when whole is 0.\"\"\"\n    return f\"{part / whole * 100:.1f}%\"\n",
        "    return f\"{part / whole * 100:.1f}%\"",
        "    if whole == 0:\n        return \"0.0%\"\n    return f\"{part / whole * 100:.1f}%\"",
        "from percent import percent\n\n\nclass PercentTest(unittest.TestCase):\n    def test_zero_whole(self):\n        self.assertEqual(percent(3, 0), '0.0%')\n",
        "from percent import percent\nassert percent(1, 3) == '33.3%'\nassert percent(0, 0) == '0.0%'\nassert percent(2, 2) == '100.0%'\n",
    ),
)


def _bug_files():
    files = {"README.md": "# utils\n\n六个小模块，每个都有对应的 test_*.py。\n"}
    for module, source, _old, _new, test, _hidden in BUGS:
        files[f"{module}.py"] = source
        files[f"test_{module}.py"] = "import unittest\n\n" + test
    return files


def _bug_solution():
    steps = [tool("run_shell", command=f"{CHECK} 2>&1 | tail -20")]
    steps += [tool("read_file", path=f"{module}.py") for module, *_rest in BUGS]
    steps += [tool("patch_file", path=f"{module}.py", old_text=old, new_text=new) for module, _src, old, new, _t, _h in BUGS]
    steps.append(tool("run_shell", command=f"{CHECK} 2>&1 | tail -3"))
    steps.append(final("六个模块的 bug 都修好了，测试通过。"))
    return tuple(steps)


# ---- 题三：删掉的 config.get 要按类型换成新的读取函数 ---------------------------------

CONFIG_PY = '''"""Typed settings. `get(key, default)` was removed in 2.0: use the typed getters."""

SETTINGS = {
    "pool_size": "8",
    "debug": "yes",
    "region": "ap-east",
    "sample_ratio": "0.25",
    "retries": "5",
    "dry_run": "no",
    "banner": "Welcome",
    "max_upload_mb": "64",
}
TRUE_WORDS = {"1", "true", "yes", "on"}


def _raw(key):
    return SETTINGS.get(key)


def get_str(key, default):
    value = _raw(key)
    return default if value is None else value


def get_int(key, default):
    value = _raw(key)
    return default if value is None else int(value)


def get_float(key, default):
    value = _raw(key)
    return default if value is None else float(value)


def get_bool(key, default):
    value = _raw(key)
    return default if value is None else value.strip().lower() in TRUE_WORDS
'''

# (模块, 设置名, 旧调用的默认值字面量, 正确的读取函数, 用法, 期望值)
CALL_SITES = (
    ("pool", "pool_size", "4", "get_int", "def size():\n    return {call} * 2\n", ("size()", 16)),
    ("logging_setup", "debug", "False", "get_bool", "def level():\n    return \"DEBUG\" if {call} else \"INFO\"\n", ("level()", "DEBUG")),
    ("regions", "region", "\"eu-west\"", "get_str", "def endpoint():\n    return \"https://\" + {call} + \".api.example\"\n", ("endpoint()", "https://ap-east.api.example")),
    ("sampling", "sample_ratio", "1.0", "get_float", "def keep(count):\n    return int(count * {call})\n", ("keep(100)", 25)),
    ("http_retry", "retries", "3", "get_int", "def attempts():\n    return list(range({call}))\n", ("attempts()", [0, 1, 2, 3, 4])),
    ("deploy", "dry_run", "True", "get_bool", "def mode():\n    return \"plan\" if {call} else \"apply\"\n", ("mode()", "apply")),
    ("greeting", "banner", "\"Hi\"", "get_str", "def line(name):\n    return {call} + \", \" + name\n", ("line('ada')", "Welcome, ada")),
    ("uploads", "max_upload_mb", "10", "get_int", "def limit_bytes():\n    return {call} * 1024 * 1024\n", ("limit_bytes()", 64 * 1024 * 1024)),
)


def _old_call(key, default):
    return f'config.get("{key}", {default})'


def _new_call(key, default, getter):
    return f'config.{getter}("{key}", {default})'


def _config_files():
    files = {
        "config.py": CONFIG_PY,
        "README.md": "# service\n\n配置读取在 2.0 改成了带类型的函数（`get_str` / `get_int` / `get_float` / `get_bool`），旧的 `config.get` 已删除。\n",
    }
    for module, key, default, _getter, body, _expect in CALL_SITES:
        files[f"{module}.py"] = "import config\n\n\n" + body.format(call=_old_call(key, default))
    cases = "\n".join(
        f"    def test_{module}(self):\n        import {module}\n\n        self.assertEqual({module}.{expr}, {expected!r})\n"
        for module, _key, _default, _getter, _body, (expr, expected) in CALL_SITES
    )
    files["test_service.py"] = "import unittest\n\n\nclass ServiceTest(unittest.TestCase):\n" + cases
    return files


VERIFY_CONFIG_PY = '''import importlib
import pathlib
import re

import config

SITES = %r

for module, key, default, getter in SITES:
    source = pathlib.Path(module + ".py").read_text(encoding="utf-8")
    assert "config.get(" not in source, module
    assert re.search(r"config\\." + getter + r"\\(\\s*[\\"']" + key, source), (module, getter)

# 设置缺失时要回到原来的默认值（类型也对）
saved = dict(config.SETTINGS)
config.SETTINGS.clear()
assert importlib.import_module("pool").size() == 8
assert importlib.import_module("logging_setup").level() == "INFO"
assert importlib.import_module("deploy").mode() == "plan"
assert importlib.import_module("sampling").keep(10) == 10
assert importlib.import_module("greeting").line("x") == "Hi, x"
config.SETTINGS.update(saved)
''' % ([(module, key, default, getter) for module, key, default, getter, _body, _expect in CALL_SITES],)


def _config_solution():
    steps = [tool("run_shell", command=f"{CHECK} 2>&1 | tail -5"), tool("read_file", path="config.py")]
    steps += [tool("read_file", path=f"{module}.py") for module, *_rest in CALL_SITES]
    steps += [
        tool("patch_file", path=f"{module}.py", old_text=_old_call(key, default), new_text=_new_call(key, default, getter))
        for module, key, default, getter, _body, _expect in CALL_SITES
    ]
    steps.append(tool("run_shell", command=f"{CHECK} 2>&1 | tail -3"))
    steps.append(final("八处 config.get 都按默认值的类型换成了 get_int / get_bool / get_str / get_float，测试通过。"))
    return tuple(steps)


SUFFIX = f"运行 `{CHECK_TEXT}` 检查，全部通过才算完成。不要修改测试文件。"

GOAL_TASKS = [
    BenchTask(
        id="implement_toolkit",
        category="多文件",
        title="按 docstring 实现分散在六个模块里的函数",
        prompt="这个仓库里六个模块的函数都还是 NotImplementedError，请按各自 docstring 的规格全部实现。" + SUFFIX,
        files=_toolkit_files(),
        hidden_files={"verify_toolkit.py": _toolkit_verifier()},
        frozen=tuple(f"test_{module}.py" for module, *_rest in TOOLKIT),
        verifier=f"{CHECK} && {PY} verify_toolkit.py",
        goal_check=CHECK,
        step_budget=14,
        solution=_toolkit_solution(),
    ),
    BenchTask(
        id="fix_six_bugs",
        category="多文件",
        title="六个模块各有一个让测试失败的 bug",
        prompt="测试有一批失败，原因分散在好几个模块里，请逐个修好；修的时候照各函数 docstring 的说明来。" + SUFFIX,
        files=_bug_files(),
        hidden_files={"verify_bugs.py": "".join(hidden for *_rest, hidden in BUGS)},
        frozen=tuple(f"test_{module}.py" for module, *_rest in BUGS),
        verifier=f"{CHECK} && {PY} verify_bugs.py",
        goal_check=CHECK,
        step_budget=14,
        solution=_bug_solution(),
    ),
    BenchTask(
        id="migrate_typed_config",
        category="多文件",
        title="删掉的 config.get 要按默认值类型换成对应的读取函数",
        prompt=(
            "config.py 升级到 2.0 后删掉了 `config.get(key, default)`，项目里还在用它的地方全坏了。"
            "请把每一处换成和原默认值类型对应的读取函数（get_str / get_int / get_float / get_bool），"
            "默认值保持原样。" + SUFFIX
        ),
        files=_config_files(),
        hidden_files={"verify_config_sites.py": VERIFY_CONFIG_PY},
        frozen=("test_service.py", "config.py"),
        verifier=f"{CHECK} && {PY} verify_config_sites.py",
        goal_check=CHECK,
        step_budget=19,
        solution=_config_solution(),
    ),
]
