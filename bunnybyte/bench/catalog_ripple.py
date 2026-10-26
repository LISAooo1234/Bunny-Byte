"""bench 任务集（三）：改一处会牵连别处的任务。

提示词只点名一个失败的测试，让它通过的“最顺手”改法却会让项目里另一个没被提到的测试变红：
只跑被点名那条测试的模型会把回归交出去，隐藏 verifier 判负；验证闸门会替模型跑一遍项目全部测试，把失败回灌给它修复。
这类任务是专门为此构造的陷阱，通过率的差别只说明闸门能拦住回归，不代表日常任务整体通过率的提升。
"""

from .tasks import PY, BenchTask, final, tool

SLUGS_PY = '''"""Turn article titles into URL slugs."""


def slugify(title):
    """Lowercase the title and join its words with hyphens."""
    return "-".join(title.lower().split())
'''

TEST_SLUGS_PY = '''import unittest

from slugs import slugify


class SlugTest(unittest.TestCase):
    def test_words_are_joined(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_extra_spaces_collapse(self):
        self.assertEqual(slugify("  Many   Spaces "), "many-spaces")

    def test_punctuation_is_dropped(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("Wait... what?"), "wait-what")

    def test_hyphens_are_kept(self):
        self.assertEqual(slugify("state-of-the-art"), "state-of-the-art")
'''

SITEMAP_PY = '''"""Build sitemap paths for published pages."""

from slugs import slugify


def page_path(title):
    return "/p/" + slugify(title)
'''

TEST_SITEMAP_PY = '''import unittest

from sitemap import page_path


class SitemapTest(unittest.TestCase):
    def test_chinese_page_path(self):
        self.assertEqual(page_path("兔子 笔记"), "/p/兔子-笔记")

    def test_accented_page_path(self):
        self.assertEqual(page_path("Café Au Lait"), "/p/café-au-lait")
'''

CONFIG_PY = '''"""Site settings."""

SITE_NAME = "Bunny Notes"
PAGE_SIZE = 20
'''

RENDER_PY = '''"""Render a page as HTML."""

from config import SITE_NAME


def render_page(title, body):
    return f"<title>{title} - {SITE_NAME}</title><main>{body}</main>"
'''

FEED_PY = '''"""Build the list of recent pages for the feed."""

from config import PAGE_SIZE


def recent_titles(titles):
    return list(titles)[-PAGE_SIZE:]
'''

VERIFY_SLUGS_PY = '''import unittest

from slugs import slugify


class HiddenSlugTest(unittest.TestCase):
    def test_more_punctuation_is_dropped(self):
        self.assertEqual(slugify("Rust (2021) Notes"), "rust-2021-notes")
'''

LIMITS_PY = '''"""Shared limits for network calls."""

DEFAULT_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
'''

CLIENT_PY = '''"""HTTP client options."""

from limits import DEFAULT_TIMEOUT, MAX_RETRIES


def request_options(timeout=None):
    return {"timeout": timeout or DEFAULT_TIMEOUT, "retries": MAX_RETRIES}
'''

LEGACY_PY = '''"""Old upload path that predates limits.py."""

TIMEOUT = 30


def upload_options():
    return {"timeout": TIMEOUT, "retries": 1}
'''

TEST_CLIENT_PY = '''import unittest

import client


class ClientTest(unittest.TestCase):
    def test_default_timeout(self):
        self.assertEqual(client.request_options()["timeout"], 60)

    def test_explicit_timeout_wins(self):
        self.assertEqual(client.request_options(5)["timeout"], 5)
'''

TEST_LEGACY_PY = '''import unittest

import legacy
import limits


class LegacyTest(unittest.TestCase):
    def test_uploads_follow_the_default(self):
        self.assertEqual(legacy.upload_options()["timeout"], limits.DEFAULT_TIMEOUT)
'''

VERIFY_LIMITS_PY = '''import unittest

import client
import legacy
import limits


class HiddenLimitsTest(unittest.TestCase):
    def test_every_default_is_sixty(self):
        self.assertEqual(limits.DEFAULT_TIMEOUT, 60)
        self.assertEqual(client.request_options(), {"timeout": 60, "retries": 3})
        self.assertEqual(legacy.upload_options(), {"timeout": 60, "retries": 1})

    def test_explicit_timeout_still_wins(self):
        self.assertEqual(client.request_options(5)["timeout"], 5)
'''

RIPPLE_TASKS = [
    BenchTask(
        id="fix_keeps_cjk_slug",
        category="改动牵连",
        title="让被点名的测试通过，同时不能弄坏另一个文件里守着中文和重音字母的测试",
        prompt=(
            "test_slugs.py 里的 test_punctuation_is_dropped 失败了：slugify('Hello, World!') "
            "得到 'hello,-world!'，应为 'hello-world'。运行 `python3 -m unittest test_slugs` 可以复现。"
            "请修复 slugs.py，不要修改测试文件。"
        ),
        files={
            "slugs.py": SLUGS_PY,
            "sitemap.py": SITEMAP_PY,
            "config.py": CONFIG_PY,
            "render.py": RENDER_PY,
            "feed.py": FEED_PY,
            "test_slugs.py": TEST_SLUGS_PY,
            "test_sitemap.py": TEST_SITEMAP_PY,
        },
        hidden_files={"verify_slugs.py": VERIFY_SLUGS_PY},
        frozen=("test_slugs.py", "test_sitemap.py"),
        verifier=f"{PY} -m unittest -q test_slugs test_sitemap verify_slugs",
        step_budget=9,
        solution=(
            tool("run_shell", command=f"{PY} -m unittest test_slugs 2>&1"),
            tool("read_file", path="slugs.py"),
            tool(
                "patch_file",
                path="slugs.py",
                old_text='"""Turn article titles into URL slugs."""\n',
                new_text='"""Turn article titles into URL slugs."""\n\nimport re\n',
            ),
            tool(
                "patch_file",
                path="slugs.py",
                old_text='    return "-".join(title.lower().split())',
                new_text='    cleaned = re.sub(r"[^\\w\\s-]", "", title.lower())\n    return "-".join(cleaned.split())',
            ),
            tool("run_shell", command=f"{PY} -m unittest -q 2>&1"),
            final("slugify 现在用 Unicode 感知的正则去掉标点，中文和重音字母保留，全部测试通过。"),
        ),
    ),
    BenchTask(
        id="change_default_everywhere",
        category="改动牵连",
        title="让被点名的测试通过，同时不能漏掉写死了旧默认值的另一处",
        prompt=(
            "test_client.py 里的 test_default_timeout 失败了：默认请求超时应该是 60 秒，现在是 30 秒。"
            "运行 `python3 -m unittest test_client` 可以复现。请修复，不要修改测试文件。"
        ),
        files={
            "limits.py": LIMITS_PY,
            "client.py": CLIENT_PY,
            "legacy.py": LEGACY_PY,
            "test_client.py": TEST_CLIENT_PY,
            "test_legacy.py": TEST_LEGACY_PY,
        },
        hidden_files={"verify_limits.py": VERIFY_LIMITS_PY},
        frozen=("test_client.py", "test_legacy.py"),
        verifier=f"{PY} -m unittest -q test_client test_legacy verify_limits",
        step_budget=10,
        solution=(
            tool("run_shell", command=f"{PY} -m unittest test_client 2>&1"),
            tool("search", pattern="30", path="."),
            tool("read_file", path="limits.py"),
            tool("patch_file", path="limits.py", old_text="DEFAULT_TIMEOUT = 30", new_text="DEFAULT_TIMEOUT = 60"),
            tool("read_file", path="legacy.py"),
            tool("patch_file", path="legacy.py", old_text="TIMEOUT = 30", new_text="TIMEOUT = 60"),
            tool("run_shell", command=f"{PY} -m unittest -q 2>&1"),
            final("limits.py 和 legacy.py 的默认超时都改成了 60 秒，全部测试通过。"),
        ),
    ),
]
