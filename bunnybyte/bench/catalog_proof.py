"""bench 任务集（五，单独的 proof 套件）：修 bug 并补回归测试，但最顺手的测试抓不住这个 bug。

提示词只描述用户看到的症状，复现条件要 agent 自己找：清空前算过一次总价、两篇文章都用默认标签、记录数不是每页条数的整数倍。
只测“看起来相关”的路径时，新测试在修好和没修好的代码上都能通过（修复证明称之为“没测到”），隐藏 verifier 判负。
每个仓库都带一个原有的 unittest 测试，保证验证闸门能跑通、修复证明有机会接着跑。
默认 `bunny bench` 不跑这一套，用 `--suite proof` 选择，配合 `--fix-proof report|repair` 衡量修复证明。
"""

from .regression_check import regression_verifier
from .tasks import PY, BenchTask, final, tool

CART_PY = '''"""Shopping cart used by the checkout page."""


class Cart:
    def __init__(self):
        self.items = []
        self._total = None

    def add(self, name, price, qty=1):
        self.items.append((name, price, qty))
        self._total = None

    def total(self):
        """Sum of price * qty, cached until the cart changes."""
        if self._total is None:
            self._total = round(sum(price * qty for _, price, qty in self.items), 2)
        return self._total

    def clear(self):
        self.items = []
'''

TEST_CART_PY = '''import unittest

from cart import Cart


class CartTest(unittest.TestCase):
    def test_total(self):
        cart = Cart()
        cart.add("tea", 3.5, 2)
        cart.add("cake", 4.0)
        self.assertEqual(cart.total(), 11.0)
'''

CART_CHECKS = '''
from cart import Cart  # noqa: E402

cart = Cart()
cart.add("tea", 3.5, 2)
assert cart.total() == 7.0
cart.clear()
assert cart.items == [] and cart.total() == 0, cart.total()
cart.add("cake", 4.0)
assert cart.total() == 4.0, cart.total()
'''

POSTS_PY = '''"""Blog posts and their tags."""


def new_post(title, tags=[]):
    """Create a post; tags defaults to an empty list."""
    return {"title": title.strip(), "tags": tags}


def tag_post(post, tag):
    """Add a normalized tag once."""
    tag = tag.strip().lower()
    if tag and tag not in post["tags"]:
        post["tags"].append(tag)
    return post
'''

TEST_POSTS_PY = '''import unittest

from posts import new_post


class PostsTest(unittest.TestCase):
    def test_title_is_stripped(self):
        self.assertEqual(new_post("  Hello ")["title"], "Hello")
'''

POSTS_CHECKS = '''
from posts import new_post, tag_post  # noqa: E402

first = tag_post(new_post("a"), " Python ")
second = new_post("b")
assert first["tags"] == ["python"], first
assert second["tags"] == [], second
given = ["x"]
assert new_post("c", given)["tags"] == ["x"]
'''

PAGES_PY = '''"""Split records into pages for the CSV export."""


def paginate(items, per_page):
    """Return a list of pages, each a list of at most per_page items."""
    if per_page < 1:
        raise ValueError("per_page must be >= 1")
    pages = []
    for start in range(0, len(items) - per_page + 1, per_page):
        pages.append(items[start:start + per_page])
    return pages
'''

EXPORT_PY = '''"""Export records page by page."""

from pages import paginate


def export_rows(rows, per_page=50):
    lines = []
    for number, page in enumerate(paginate(rows, per_page), start=1):
        lines.extend(f"{number},{row}" for row in page)
    return lines
'''

TEST_PAGES_PY = '''import unittest

from pages import paginate


class PagesTest(unittest.TestCase):
    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            paginate([1], 0)

    def test_empty(self):
        self.assertEqual(paginate([], 3), [])
'''

PAGES_CHECKS = '''
from export import export_rows  # noqa: E402
from pages import paginate  # noqa: E402

assert paginate([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]], paginate([1, 2, 3, 4, 5], 2)
assert paginate([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]
assert paginate([1], 3) == [[1]]
assert paginate([], 3) == []
assert len(export_rows(list(range(120)))) == 120
'''


def _real_test(module, imports, body):
    return f"import unittest\n\nfrom {module} import {imports}\n\n\nclass RegressionTest(unittest.TestCase):\n{body}"


CART_REAL_TEST = _real_test(
    "cart",
    "Cart",
    "    def test_clear_after_total(self):\n"
    "        cart = Cart()\n"
    "        cart.add(\"tea\", 3.5)\n"
    "        cart.total()\n"
    "        cart.clear()\n"
    "        self.assertEqual(cart.total(), 0)\n",
)

# 裸 pytest 函数：判分和修复证明都得用 pytest 跑它（离线测试覆盖这条路径）
POSTS_REAL_TEST = (
    "from posts import new_post, tag_post\n\n\n"
    "def test_default_tags_are_not_shared():\n"
    "    tag_post(new_post(\"a\"), \"x\")\n"
    "    assert new_post(\"b\")[\"tags\"] == []\n"
)

PAGES_REAL_TEST = _real_test(
    "pages",
    "paginate",
    "    def test_partial_last_page(self):\n"
    "        self.assertEqual(paginate([1, 2, 3], 2), [[1, 2], [3]])\n",
)

PROOF_TASKS = [
    BenchTask(
        id="cart_total_after_clear",
        category="修复证明",
        title="清空购物车后总价不归零：修好并补一个能抓住它的测试",
        prompt="用户反馈：结账页清空购物车以后，总价有时还显示清空前的金额。请修好 cart.py，并补一个回归测试。",
        files={"cart.py": CART_PY, "test_cart.py": TEST_CART_PY},
        hidden_files={"verify_cart.py": regression_verifier("cart.py", CART_PY, {"test_cart.py": TEST_CART_PY}, CART_CHECKS)},
        verifier=f"{PY} verify_cart.py",
        step_budget=10,
        solution=(
            tool("read_file", path="cart.py"),
            tool("patch_file", path="cart.py", old_text="    def clear(self):\n        self.items = []\n", new_text="    def clear(self):\n        self.items = []\n        self._total = None\n"),
            tool("write_file", path="test_cart_clear.py", content=CART_REAL_TEST),
            tool("run_shell", command=f"{PY} -m unittest -q test_cart_clear 2>&1"),
            final("clear() 现在会清掉缓存的总价；新增 test_cart_clear.py（先算总价再清空），修复前会失败。"),
        ),
    ),
    BenchTask(
        id="posts_shared_default_tags",
        category="修复证明",
        title="新文章自带别的文章的标签：修好并补一个能抓住它的测试",
        prompt="用户反馈：新建的文章有时会莫名带上别的文章的标签。请修好 posts.py，并补一个回归测试。",
        files={"posts.py": POSTS_PY, "test_posts.py": TEST_POSTS_PY},
        hidden_files={"verify_posts.py": regression_verifier("posts.py", POSTS_PY, {"test_posts.py": TEST_POSTS_PY}, POSTS_CHECKS)},
        verifier=f"{PY} verify_posts.py",
        step_budget=10,
        solution=(
            tool("read_file", path="posts.py"),
            tool(
                "patch_file",
                path="posts.py",
                old_text='def new_post(title, tags=[]):\n    """Create a post; tags defaults to an empty list."""\n    return {"title": title.strip(), "tags": tags}',
                new_text='def new_post(title, tags=None):\n    """Create a post; tags defaults to an empty list."""\n    return {"title": title.strip(), "tags": [] if tags is None else tags}',
            ),
            tool("write_file", path="tests/test_default_tags.py", content=POSTS_REAL_TEST),
            tool("run_shell", command=f"{PY} -m pytest -q -p no:cacheprovider tests/test_default_tags.py 2>&1"),
            final("默认标签改成每次新建空列表；新增 tests/test_default_tags.py，修复前会失败。"),
        ),
    ),
    BenchTask(
        id="export_drops_last_page",
        category="修复证明",
        title="导出时少了几条记录：找到原因、修好并补一个能抓住它的测试",
        prompt="用户反馈：CSV 导出（export.py）经常少几条记录，不是每次都少。请找到原因修好，并补一个回归测试。",
        files={"pages.py": PAGES_PY, "export.py": EXPORT_PY, "test_pages.py": TEST_PAGES_PY},
        hidden_files={"verify_pages.py": regression_verifier("pages.py", PAGES_PY, {"test_pages.py": TEST_PAGES_PY}, PAGES_CHECKS)},
        verifier=f"{PY} verify_pages.py",
        step_budget=11,
        solution=(
            tool("read_file", path="export.py"),
            tool("read_file", path="pages.py"),
            tool(
                "patch_file",
                path="pages.py",
                old_text="range(0, len(items) - per_page + 1, per_page)",
                new_text="range(0, len(items), per_page)",
            ),
            tool("write_file", path="test_pages_partial.py", content=PAGES_REAL_TEST),
            tool("run_shell", command=f"{PY} -m unittest -q test_pages_partial 2>&1"),
            final("分页循环漏掉了不满一页的最后一页；改成按 len(items) 循环，新增 test_pages_partial.py，修复前会失败。"),
        ),
    ),
]
