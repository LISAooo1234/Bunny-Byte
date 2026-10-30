"""bench 任务集（四，单独的 spec 套件）：测试全过、需求却漏了一半的任务。

提示词只说要修什么；仓库的 AGENTS.md 里写着团队约定（修 bug 要补回归测试、要写 CHANGELOG、选项改名要保留旧名字）。
这些约定会进 agent 的系统提示词，但只盯着“测试变绿”的模型常常把它们忘掉，交出半份活，隐藏 verifier 判负；
独立审查（critic）对照需求、项目约定和 diff 时能看出缺了什么。
默认 `bunny bench` 不跑这一套（任务集指纹和历史基线保持不变），用 `--suite spec` 或 `--tasks` 选择。
"""

from .tasks import PY, BenchTask, final, tool

PRICING_PY = '''"""Price helpers for the checkout page."""


def apply_discount(price, percent):
    """Return price after a percent discount (0-100), rounded to cents."""
    return round(price - price * percent / 10, 2)
'''

TEST_PRICING_PY = '''import unittest

from pricing import apply_discount


class PricingTest(unittest.TestCase):
    def test_no_discount_keeps_the_price(self):
        self.assertEqual(apply_discount(50, 0), 50)
'''

# 用户新加或改过的测试文件，必须在原来那版有 bug 的 pricing.py 上失败，在现在的代码上通过。
VERIFY_REGRESSION_PY = '''import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

BUGGY = {buggy!r}
ORIGINAL_TEST = {original_test!r}

here = pathlib.Path(".")
added = [
    path for path in sorted(here.rglob("test_*.py"))
    if ".bunnybyte" not in path.parts
    and not (path.as_posix() == "test_pricing.py" and path.read_text(encoding="utf-8") == ORIGINAL_TEST)
]
assert added, "没有新增或修改任何测试文件：需求要求加一个回归测试"


def run(cwd, paths):
    # 子目录里的测试（tests/test_x.py）也要能 import 根目录的 pricing
    env = dict(os.environ, PYTHONPATH=str(pathlib.Path(cwd).resolve()))
    results = [
        subprocess.run([sys.executable, "-B", str(path)], cwd=cwd, capture_output=True, text=True, env=env)
        if "unittest" not in path.read_text(encoding="utf-8")
        else subprocess.run(
            [sys.executable, "-B", "-m", "unittest", "-q", str(path).replace("/", ".")[:-3]],
            cwd=cwd, capture_output=True, text=True, env=env,
        )
        for path in paths
    ]
    return max(item.returncode for item in results), "".join(item.stderr for item in results)


code, err = run(here, added)
assert code == 0, "新测试在修好的代码上没有通过：" + err[-300:]
with tempfile.TemporaryDirectory() as scratch:
    for path in added:
        (pathlib.Path(scratch) / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, pathlib.Path(scratch) / path)
    pathlib.Path(scratch, "pricing.py").write_text(BUGGY, encoding="utf-8")
    old_code, _ = run(pathlib.Path(scratch), [pathlib.Path(p) for p in map(str, added)])
assert old_code != 0, "新测试在原来有 bug 的代码上也通过了，抓不住这个 bug"

from pricing import apply_discount  # noqa: E402

assert apply_discount(80, 25) == 60.0, apply_discount(80, 25)
assert apply_discount(19.99, 10) == 17.99, apply_discount(19.99, 10)
'''.format(buggy=PRICING_PY, original_test=TEST_PRICING_PY)

TEXTSTATS_PY = '''"""Small text statistics used by the editor status bar."""


def word_count(text):
    """Number of words separated by whitespace."""
    return len(text.split(" "))


def char_count(text):
    return len(text)
'''

TEST_TEXTSTATS_PY = '''import unittest

from textstats import char_count, word_count


class TextStatsTest(unittest.TestCase):
    def test_simple_sentence(self):
        self.assertEqual(word_count("one two three"), 3)

    def test_multiple_spaces(self):
        self.assertEqual(word_count("one  two   three"), 3)

    def test_chars(self):
        self.assertEqual(char_count("abc"), 3)
'''

CHANGELOG_MD = """# Changelog

## Unreleased

## 1.2.0
- Added `char_count`.
"""

VERIFY_CHANGELOG_PY = '''import pathlib

from textstats import word_count

assert word_count("  leading and trailing  ") == 3, word_count("  leading and trailing  ")
assert word_count("tabs\\tand\\nnewlines") == 3, word_count("tabs\\tand\\nnewlines")
assert word_count("") == 0, word_count("")

text = pathlib.Path("CHANGELOG.md").read_text(encoding="utf-8")
assert "## Unreleased" in text and "## 1.2.0" in text, "CHANGELOG.md 的版本标题被改坏了"
unreleased = text.split("## Unreleased", 1)[1].split("## 1.2.0", 1)[0]
entries = [line for line in unreleased.splitlines() if line.strip().startswith("- ") and len(line.strip()) > 4]
assert entries, "CHANGELOG.md 的 Unreleased 下面没有新增说明"
'''

REPORT_PY = '''"""Print the nightly report line."""

import argparse
import sys


def build_parser():
    parser = argparse.ArgumentParser(prog="report")
    parser.add_argument("--colour", action="store_true", help="colour the output green")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    text = "report ready"
    if args.colour:
        text = "\\x1b[32m" + text + "\\x1b[0m"
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

TEST_REPORT_PY = '''import contextlib
import io
import unittest

from report import main


def output(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        main(argv)
    return buffer.getvalue()


class ReportTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(output([]), "report ready\\n")

    def test_color_flag(self):
        self.assertIn("\\x1b[32m", output(["--color"]))
'''

REPORT_README = """# report

Print the nightly report line:

    python3 report.py --colour
"""

VERIFY_ALIAS_PY = '''import contextlib
import io
import pathlib
import re

from report import build_parser, main


def output(argv):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        main(argv)
    return buffer.getvalue()


assert "\\x1b[32m" in output(["--color"]), "--color 不起作用"
try:
    legacy = output(["--colour"])
except SystemExit:
    raise AssertionError("老选项 --colour 不能用了：需求要求保留它作为别名") from None
assert "\\x1b[32m" in legacy, "--colour 别名没有上色"
assert output([]) == "report ready\\n"
assert re.search(r"--color\\b", build_parser().format_help()), "--help 里没有 --color"
readme = pathlib.Path("README.md").read_text(encoding="utf-8")
assert re.search(r"--color\\b", readme), "README.md 的用法没有改成 --color"
'''

AGENTS_MD = """# Team conventions

- Every bug fix comes with a regression test in a `test_*.py` file that fails on the old code.
- Every user-visible fix or change gets a line starting with `- ` under `## Unreleased` in CHANGELOG.md (if the repo has one).
- Never remove or rename a command-line option without keeping the old spelling working as an alias.
- When a command-line option changes, update the usage in README.md.
- Do not edit existing tests unless the task asks you to.
"""

SPEC_TASKS = [
    BenchTask(
        id="fix_with_regression_test",
        category="需求遗漏",
        title="修 bug，并按 AGENTS.md 约定补一个能抓住它的回归测试",
        prompt="pricing.py 的 apply_discount(80, 25) 应该返回 60.0，现在返回 30.0，修一下。",
        files={"pricing.py": PRICING_PY, "test_pricing.py": TEST_PRICING_PY, "AGENTS.md": AGENTS_MD},
        hidden_files={"verify_regression.py": VERIFY_REGRESSION_PY},
        verifier=f"{PY} -m unittest -q && {PY} verify_regression.py",
        step_budget=8,
        solution=(
            tool("read_file", path="pricing.py"),
            tool("patch_file", path="pricing.py", old_text="percent / 10,", new_text="percent / 100,"),
            tool(
                "write_file",
                path="tests/test_discount_regression.py",
                content=(
                    "import unittest\n\nfrom pricing import apply_discount\n\n\n"
                    "class DiscountRegressionTest(unittest.TestCase):\n"
                    "    def test_quarter_off(self):\n"
                    "        self.assertEqual(apply_discount(80, 25), 60.0)\n"
                ),
            ),
            tool("run_shell", command=f"PYTHONPATH=. {PY} -m unittest discover -q -s tests -t . 2>&1"),
            final("折扣改成按百分之一计算，并新增 tests/test_discount_regression.py 回归测试（修复前会失败），测试通过。"),
        ),
    ),
    BenchTask(
        id="fix_and_update_changelog",
        category="需求遗漏",
        title="修复失败的测试，并按 AGENTS.md 约定写 CHANGELOG",
        prompt=(
            "test_textstats.py 里的 test_multiple_spaces 失败了，运行 `python3 -m unittest test_textstats` 可以复现，修一下。"
        ),
        files={
            "textstats.py": TEXTSTATS_PY,
            "test_textstats.py": TEST_TEXTSTATS_PY,
            "CHANGELOG.md": CHANGELOG_MD,
            "AGENTS.md": AGENTS_MD.replace("regression test in a `test_*.py` file", "test"),
        },
        hidden_files={"verify_changelog.py": VERIFY_CHANGELOG_PY},
        frozen=("test_textstats.py",),
        verifier=f"{PY} -m unittest -q test_textstats && {PY} verify_changelog.py",
        step_budget=8,
        solution=(
            tool("read_file", path="textstats.py"),
            tool("patch_file", path="textstats.py", old_text='text.split(" ")', new_text="text.split()"),
            tool("read_file", path="CHANGELOG.md"),
            tool(
                "patch_file",
                path="CHANGELOG.md",
                old_text="## Unreleased\n",
                new_text="## Unreleased\n- Fixed `word_count` counting empty strings between repeated spaces.\n",
            ),
            tool("run_shell", command=f"{PY} -m unittest -q test_textstats 2>&1"),
            final("word_count 改用 split()，连续空白不再多算；CHANGELOG.md 的 Unreleased 下加了说明，测试通过。"),
        ),
    ),
    BenchTask(
        id="rename_option_keep_alias",
        category="需求遗漏",
        title="改选项名，并按 AGENTS.md 约定保留旧名字做别名、更新 README",
        prompt="把 report.py 的 `--colour` 选项改成美式拼写 `--color`，test_report.py 里已经有对应的测试。",
        files={"report.py": REPORT_PY, "test_report.py": TEST_REPORT_PY, "README.md": REPORT_README, "AGENTS.md": AGENTS_MD},
        hidden_files={"verify_alias.py": VERIFY_ALIAS_PY},
        frozen=("test_report.py",),
        verifier=f"{PY} -m unittest -q test_report && {PY} verify_alias.py",
        step_budget=8,
        solution=(
            tool("read_file", path="report.py"),
            tool(
                "patch_file",
                path="report.py",
                old_text='parser.add_argument("--colour", action="store_true", help="colour the output green")',
                new_text='parser.add_argument("--color", "--colour", dest="colour", action="store_true", help="color the output green")',
            ),
            tool("read_file", path="README.md"),
            tool("patch_file", path="README.md", old_text="report.py --colour", new_text="report.py --color"),
            tool("run_shell", command=f"{PY} -m unittest -q test_report 2>&1"),
            final("选项改为 --color，--colour 保留为别名；README 用法已更新，测试通过。"),
        ),
    ),
]
