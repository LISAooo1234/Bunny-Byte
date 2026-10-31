"""隐藏 verifier 的共用部分：agent 新增或改过的测试必须在现在的代码上通过、在原来有 bug 的模块上失败。

选运行方式的规则和修复证明一致（`core/fix_proof._wants_pytest`）：不带 unittest 的裸 `def test_` 用 pytest，
写了 unittest 的按文件路径用 `-m unittest`（`tests/` 没有 `__init__.py` 时顶层 discover 收集不到），其余当断言脚本直接跑。
测试文件的认法（`test_*.py` 和 `*_test.py`）和“旧代码上只因缺少接口而失败 = 弱、不算抓住”也和修复证明共用同一组正则。
否则判分会和修复证明在构造上就不一致：裸 pytest 函数当脚本跑只是定义了函数、退出码 0，会被误判成“抓不住 bug”。
"""

TEMPLATE = '''import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

MODULE = {module!r}
BUGGY = {buggy!r}
ORIGINALS = {originals!r}
TEST_FILE = re.compile({test_file!r})
WEAK = re.compile({weak!r})
ASSERTION = re.compile({assertion!r}, re.MULTILINE)
SKIP_PARTS = {{".git", ".bunnybyte", ".home", "__pycache__"}}

here = pathlib.Path(".").resolve()
added = [
    path.relative_to(here) for path in sorted(here.rglob("*.py"))
    if TEST_FILE.search(path.relative_to(here).as_posix())
    and not SKIP_PARTS & set(path.relative_to(here).parts)
    and ORIGINALS.get(path.relative_to(here).as_posix()) != path.read_text(encoding="utf-8")
]
assert added, "没有新增或修改任何测试文件：需求要求加一个回归测试"


def argv(root, path):
    text = (root / path).read_text(encoding="utf-8")
    if re.search(r"^\\s*def test", text, re.MULTILINE) and "unittest" not in text:
        return [sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", str(path)]
    if "unittest" in text:
        return [sys.executable, "-B", "-m", "unittest", "-q", str(path)]
    return [sys.executable, "-B", str(path)]


def run(root):
    # 子目录里的测试（tests/test_x.py）也要能 import 根目录的模块
    env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
    code, output = 0, ""
    for path in added:
        command = argv(root, path)
        done = subprocess.run(command, cwd=root, capture_output=True, text=True, env=env, timeout=60)
        text = done.stdout + done.stderr
        output += text
        if re.search(r"Ran 0 tests|no tests ran|collected 0 items", text, re.IGNORECASE):
            return -1, output  # 一个测试都没收集到，不能算“通过”也不能算“抓住了”
        if "pytest" in command and done.returncode == 2:
            return -2, output  # pytest 收集阶段就失败：几乎都是导入不了本轮新加的东西
        code = code or done.returncode
    return code, output


code, out = run(here)
assert code == 0, "新测试在修好的代码上没有通过：" + out[-300:]
with tempfile.TemporaryDirectory() as scratch:
    copy = pathlib.Path(scratch) / "repo"
    shutil.copytree(here, copy, ignore=shutil.ignore_patterns(*SKIP_PARTS, "verify_*.py"))
    (copy / MODULE).write_text(BUGGY, encoding="utf-8")
    old_code, old_out = run(copy)
assert old_code not in (0, -1), "新测试在原来有 bug 的代码上也通过了，抓不住这个 bug"
# 修复证明把“旧代码上只因缺少接口而失败”算作弱证明：这说明不了测试测到了行为，判分也不算抓住
assert old_code != -2, "新测试在原来的代码上收集失败（导入不了本轮新增的东西），说明不了它能抓住这个 bug"
assert not (WEAK.search(old_out) and not ASSERTION.search(old_out)), (
    old_out[-200:] + " ⇒ 新测试在原来的代码上只因缺少接口而失败，说明不了它能抓住这个 bug"
)  # 说明放在最后：判分只保留 stderr 的末尾
'''


def regression_verifier(module, buggy, originals, checks=""):
    """生成隐藏 verifier 脚本：module 是有 bug 的源码文件，originals 是 fixture 里原有的测试文件，checks 是追加的正确性断言。"""
    from ..core.fix_proof import ASSERT_FAILURE_RE, WEAK_RE
    from ..core.verification import TEST_FILE_RE

    return TEMPLATE.format(
        module=module,
        buggy=buggy,
        originals=dict(originals),
        test_file=TEST_FILE_RE.pattern,
        weak=WEAK_RE.pattern,
        assertion=ASSERT_FAILURE_RE.pattern,
    ) + checks
