"""bench 任务集（二）：实现 / 重构 / 只读问答 / 数据处理类。"""

from .tasks import PY, BenchTask, final, tool

TEXTUTIL_PY = '''def slugify(text):
    """Convert text to a URL slug.

    - Lowercase everything.
    - Every run of characters other than a-z and 0-9 becomes a single "-".
    - Strip leading and trailing "-".
    - Return "untitled" if nothing is left.

    >>> slugify("Hello, World!")
    'hello-world'
    >>> slugify("  --Python 3.12 Release--  ")
    'python-3-12-release'
    >>> slugify("Caf\\u00e9 au lait")
    'caf-au-lait'
    >>> slugify("!!!")
    'untitled'
    """
    raise NotImplementedError
'''

VERIFY_SLUG_PY = '''import unittest

from textutil import slugify


class HiddenSlugTest(unittest.TestCase):
    def test_cases(self):
        self.assertEqual(slugify("A  B"), "a-b")
        self.assertEqual(slugify("snake_case_name"), "snake-case-name")
        self.assertEqual(slugify(""), "untitled")
        self.assertEqual(slugify("2024/10/26"), "2024-10-26")
'''

STORE_PY = '''"""In-memory user store."""

USERS = {1: "ada", 2: "linus", 3: "grace"}


def fetch_user(user_id):
    """Return the user name for user_id (fetch_user raises KeyError if missing)."""
    return USERS[user_id]


def fetch_users():
    """Return every user name."""
    return sorted(USERS.values())
'''

API_PY = '''from store import fetch_user, fetch_users


def greet(user_id):
    return f"hi {fetch_user(user_id)}"


def describe(user_id):
    name = fetch_user(user_id)
    return f"{user_id}:{name} of {len(fetch_users())}"
'''

CLI_PY = '''import sys

from store import fetch_user


def main(argv):
    print(fetch_user(int(argv[0])))


if __name__ == "__main__":
    main(sys.argv[1:])
'''

TEST_STORE_PY = '''import unittest

from api import describe, greet
from store import fetch_user, fetch_users


class StoreTest(unittest.TestCase):
    def test_fetch_user(self):
        self.assertEqual(fetch_user(1), "ada")

    def test_fetch_users(self):
        self.assertEqual(fetch_users(), ["ada", "grace", "linus"])

    def test_api(self):
        self.assertEqual(greet(2), "hi linus")
        self.assertEqual(describe(3), "3:grace of 3")
'''

USERS_README = """# users

`store.fetch_user(user_id)` returns one name; `store.fetch_users()` lists all names.
Run `python3 cli.py 1` to print a user.
"""

VERIFY_RENAME_PY = '''import pathlib
import re
import subprocess
import sys

old = re.compile(r"\\bfetch_user\\b")
for path in pathlib.Path(".").glob("*"):
    if path.suffix in {".py", ".md"} and path.name != "verify_rename.py":
        assert not old.search(path.read_text(encoding="utf-8")), f"{path} still uses fetch_user"
store = pathlib.Path("store.py").read_text(encoding="utf-8")
assert "def load_user(" in store and "def fetch_users(" in store
assert "load_user" in pathlib.Path("README.md").read_text(encoding="utf-8")
done = subprocess.run([sys.executable, "-m", "unittest", "-q"], capture_output=True, text=True)
assert done.returncode == 0, done.stderr
'''

GREET_PY = '''import argparse


def build_greeting(name):
    return f"Hello, {name}!"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print a greeting.")
    parser.add_argument("--name", default="world", help="who to greet")
    args = parser.parse_args(argv)
    print(build_greeting(args.name))


if __name__ == "__main__":
    main()
'''

GREET_README = """# greet

Usage:

    python3 greet.py [--name NAME]

Prints `Hello, NAME!`.
"""

VERIFY_GREET_PY = '''import pathlib
import subprocess
import sys


def run(*args):
    done = subprocess.run([sys.executable, "greet.py", *args], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


assert run("--name", "bob", "--shout") == "HELLO, BOB!"
assert run("--shout") == "HELLO, WORLD!"
assert run("--name", "bob") == "Hello, bob!"
assert "--shout" in run("--help")
assert "--shout" in pathlib.Path("README.md").read_text(encoding="utf-8")
'''

RETRY_FILES = {
    "net/__init__.py": "",
    "net/retry.py": '''"""Retry policy for outgoing requests."""

MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 0.5


def delay_for(attempt):
    return BACKOFF_BASE_SECONDS * (2**attempt)
''',
    "net/settings.py": '''"""Connection pool settings."""

DEFAULT_TIMEOUT = 30
POOL_SIZE = 8
''',
    "net/client.py": '''from .retry import MAX_RETRIES, delay_for
from .settings import DEFAULT_TIMEOUT


def fetch(url, send):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return send(url, timeout=DEFAULT_TIMEOUT)
        except OSError as error:
            last_error = (error, delay_for(attempt))
    raise RuntimeError(f"giving up on {url}: {last_error}")
''',
    "docs/notes.md": """# Design notes (old)

The first prototype retried 3 times with a 2 second base delay.
The real policy lives in the net package and has changed since.
""",
}


def _jobs_csv():
    lines = ["id,status,seconds"]
    failed = 0
    for number in range(1, 61):
        if number % 7 == 0 or number % 11 == 0:
            status = "failed"
            failed += 1
        elif number % 13 == 0:
            status = "failed_retry"
        else:
            status = "ok"
        lines.append(f"{number},{status},{number % 5 + 1}")
    return "\n".join(lines) + "\n", failed


JOBS_CSV, FAILED_COUNT = _jobs_csv()

VERIFY_COUNT_PY = f'''import pathlib

text = pathlib.Path("failed_count.txt").read_text(encoding="utf-8").strip()
assert text == "{FAILED_COUNT}", text
'''

BUILD_TASKS = [
    BenchTask(
        id="implement_from_docstring",
        category="写功能",
        title="按 docstring 实现函数",
        prompt="textutil.py 里的 slugify 还没实现，请严格按照它 docstring 的说明实现。",
        files={"textutil.py": TEXTUTIL_PY},
        hidden_files={"verify_slug.py": VERIFY_SLUG_PY},
        verifier=f"{PY} -m doctest textutil.py && {PY} -m unittest -q verify_slug",
        step_budget=10,
        solution=(
            tool("read_file", path="textutil.py"),
            tool(
                "patch_file",
                path="textutil.py",
                old_text="    raise NotImplementedError",
                new_text=(
                    "    import re\n\n"
                    '    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")\n'
                    '    return slug or "untitled"'
                ),
            ),
            tool("run_shell", command=f"{PY} -m doctest textutil.py && echo doctest-ok"),
            final("slugify 已实现。"),
        ),
    ),
    BenchTask(
        id="multi_file_rename",
        category="重构",
        title="跨文件重命名函数（不误伤同前缀函数）",
        prompt=(
            "把函数 fetch_user 重命名为 load_user：同步更新所有调用处、测试和 README。"
            "注意 fetch_users（复数）是另一个函数，不要改它。改完确认测试仍然通过。"
        ),
        files={
            "store.py": STORE_PY,
            "api.py": API_PY,
            "cli.py": CLI_PY,
            "test_store.py": TEST_STORE_PY,
            "README.md": USERS_README,
        },
        hidden_files={"verify_rename.py": VERIFY_RENAME_PY},
        verifier=f"{PY} verify_rename.py",
        step_budget=16,
        solution=(
            tool("search", pattern="fetch_user", path="."),
            tool(
                "run_shell",
                command=(
                    f"{PY} -c \"import pathlib,re; [p.write_text(re.sub(r'\\bfetch_user\\b','load_user',"
                    "p.read_text())) for p in pathlib.Path('.').glob('*') if p.suffix in ('.py','.md')]\""
                ),
            ),
            tool("run_shell", command=f"{PY} -m unittest -q 2>&1"),
            final("已重命名为 load_user，fetch_users 未改动，测试通过。"),
        ),
    ),
    BenchTask(
        id="add_cli_flag",
        category="写功能",
        title="给命令行脚本加一个开关并更新文档",
        prompt=(
            "给 greet.py 增加 `--shout` 开关：加上后整句问候转成大写（例如 `HELLO, BOB!`），"
            "不加时行为保持不变。并在 README.md 的用法里补上这个开关。"
        ),
        files={"greet.py": GREET_PY, "README.md": GREET_README},
        hidden_files={"verify_greet.py": VERIFY_GREET_PY},
        verifier=f"{PY} verify_greet.py",
        step_budget=12,
        solution=(
            tool("read_file", path="greet.py"),
            tool("read_file", path="README.md"),
            tool(
                "patch_file",
                path="greet.py",
                old_text='    args = parser.parse_args(argv)\n    print(build_greeting(args.name))',
                new_text=(
                    '    parser.add_argument("--shout", action="store_true", help="upper-case the greeting")\n'
                    "    args = parser.parse_args(argv)\n"
                    "    greeting = build_greeting(args.name)\n"
                    "    print(greeting.upper() if args.shout else greeting)"
                ),
            ),
            tool(
                "patch_file",
                path="README.md",
                old_text="python3 greet.py [--name NAME]",
                new_text="python3 greet.py [--name NAME] [--shout]",
            ),
            tool("run_shell", command=f"{PY} greet.py --name bob --shout"),
            final("已添加 --shout。"),
        ),
    ),
    BenchTask(
        id="answer_codebase_question",
        category="只读问答",
        title="读代码回答问题且不改动任何文件",
        prompt=(
            "请求重试的最大次数和退避基数（秒）分别是多少？它们定义在哪个文件里？"
            "只需要回答问题，不要修改任何文件。"
        ),
        files=RETRY_FILES,
        answer_regex=(r"retry\.py", r"(?<![0-9.])5(?![0-9.])", r"(?<![0-9])0\.5(?![0-9])"),
        read_only=True,
        step_budget=8,
        solution=(
            tool("search", pattern="RETRIES", path="."),
            tool("read_file", path="net/retry.py"),
            final("定义在 net/retry.py：MAX_RETRIES = 5，BACKOFF_BASE_SECONDS = 0.5。"),
        ),
    ),
    BenchTask(
        id="count_rows_to_file",
        category="数据处理",
        title="用命令统计数据并把结果写入文件",
        prompt=(
            "统计 jobs.csv 中 status 恰好是 failed 的行数（failed_retry 不算），"
            "把这个数字单独写入 failed_count.txt。不要修改 jobs.csv。"
        ),
        files={"jobs.csv": JOBS_CSV},
        hidden_files={"verify_count.py": VERIFY_COUNT_PY},
        frozen=("jobs.csv",),
        verifier=f"{PY} verify_count.py",
        step_budget=10,
        solution=(
            tool("run_shell", command="grep -c ',failed,' jobs.csv"),
            tool("write_file", path="failed_count.txt", content=f"{FAILED_COUNT}\n"),
            final(f"共 {FAILED_COUNT} 行，已写入 failed_count.txt。"),
        ),
    ),
]
