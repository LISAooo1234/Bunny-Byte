"""bench 任务集（一）：修 bug 类。"""

from .tasks import PY, BenchTask, final, tool

STATS_PY = '''def mean(values):
    """Return the arithmetic mean of a non-empty list of numbers."""
    return sum(values) // len(values)


def median(values):
    """Return the median of a non-empty list of numbers."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 0:
        return (ordered[mid - 1] + ordered[mid]) / 2
    return ordered[mid]
'''

TEST_STATS_PY = '''import unittest

from stats import mean, median


class StatsTest(unittest.TestCase):
    def test_mean_with_fraction(self):
        self.assertAlmostEqual(mean([1, 2, 4]), 7 / 3)

    def test_mean_exact(self):
        self.assertEqual(mean([2, 4]), 3)

    def test_median_odd(self):
        self.assertEqual(median([5, 1, 3]), 3)

    def test_median_even(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)
'''

VERIFY_STATS_PY = '''import unittest

from stats import mean, median


class HiddenStatsTest(unittest.TestCase):
    def test_mean_half(self):
        self.assertEqual(mean([1, 2]), 1.5)

    def test_mean_floats(self):
        self.assertEqual(mean([0.5, 1.5]), 1.0)

    def test_median_single_and_pair(self):
        self.assertEqual(median([7]), 7)
        self.assertEqual(median([1, 2]), 1.5)
'''

SETTINGS_PY = '''"""Connection settings used by the client."""


def connect_options():
    timeout = 30
    return {"timeout": timeout, "retries": 3}


def read_options():
    timeout = 30
    return {"timeout": timeout, "retries": 3}
'''

VERIFY_SETTINGS_PY = '''import settings

assert settings.connect_options() == {"timeout": 60, "retries": 3}, settings.connect_options()
assert settings.read_options() == {"timeout": 30, "retries": 3}, settings.read_options()
'''

ORDERS_JSON = """[
  {"id": 1, "qty": 2, "unit_price": 5.0},
  {"id": 2, "qty": 1, "unit_price": 12.5},
  {"id": 3, "qty": 3, "unit_price": 3.0}
]
"""

REPORT_PY = '''import json


def load_orders(path="orders.json"):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def order_total(order):
    return order["qty"] * order["price"]


def main():
    orders = load_orders()
    total = sum(order_total(order) for order in orders)
    print(f"Total: {total}")


if __name__ == "__main__":
    main()
'''

VERIFY_REPORT_PY = '''import subprocess
import sys

out = subprocess.run([sys.executable, "report.py"], capture_output=True, text=True)
assert out.returncode == 0, out.stderr
assert out.stdout.strip() == "Total: 31.50", out.stdout
'''

FIX_TASKS = [
    BenchTask(
        id="fix_failing_test",
        category="修 bug",
        title="修复让单元测试失败的 bug",
        prompt=(
            "项目里有 stats.py 和 test_stats.py。运行 `python3 -m unittest -q` 有测试失败，"
            "请找出原因并修复 stats.py，不要修改测试文件。"
        ),
        files={"stats.py": STATS_PY, "test_stats.py": TEST_STATS_PY},
        hidden_files={"verify_stats.py": VERIFY_STATS_PY},
        frozen=("test_stats.py",),
        verifier=f"{PY} -m unittest -q test_stats verify_stats",
        step_budget=12,
        solution=(
            tool("run_shell", command=f"{PY} -m unittest -q"),
            tool("read_file", path="stats.py"),
            tool(
                "patch_file",
                path="stats.py",
                old_text="return sum(values) // len(values)",
                new_text="return sum(values) / len(values)",
            ),
            tool("run_shell", command=f"{PY} -m unittest -q 2>&1"),
            final("已把 mean 的整除改成真除法，测试通过。"),
        ),
    ),
    BenchTask(
        id="recover_from_bad_patch",
        category="工具恢复",
        title="补丁匹配到多处时自行恢复",
        prompt=(
            "把 settings.py 里 connect_options 的 timeout 改成 60，"
            "read_options 保持原样（timeout 仍为 30）。"
        ),
        files={"settings.py": SETTINGS_PY},
        hidden_files={"verify_settings.py": VERIFY_SETTINGS_PY},
        verifier=f"{PY} verify_settings.py",
        step_budget=10,
        solution=(
            tool("read_file", path="settings.py"),
            tool("patch_file", path="settings.py", old_text="timeout = 30", new_text="timeout = 60"),
            tool(
                "patch_file",
                path="settings.py",
                old_text="def connect_options():\n    timeout = 30",
                new_text="def connect_options():\n    timeout = 60",
            ),
            final("connect_options 的 timeout 已改为 60。"),
        ),
    ),
    BenchTask(
        id="fix_crash_from_traceback",
        category="修 bug",
        title="根据报错定位崩溃并满足输出要求",
        prompt=(
            "运行 `python3 report.py` 会报错。请定位并修复，让它输出 `Total: 31.50`"
            "（保留两位小数）。不要修改 orders.json。"
        ),
        files={"report.py": REPORT_PY, "orders.json": ORDERS_JSON},
        hidden_files={"verify_report.py": VERIFY_REPORT_PY},
        frozen=("orders.json",),
        verifier=f"{PY} verify_report.py",
        step_budget=12,
        solution=(
            tool("run_shell", command=f"{PY} report.py 2>&1"),
            tool("read_file", path="report.py"),
            tool(
                "patch_file",
                path="report.py",
                old_text='order["price"]',
                new_text='order["unit_price"]',
            ),
            tool("patch_file", path="report.py", old_text="{total}", new_text="{total:.2f}"),
            tool("run_shell", command=f"{PY} report.py"),
            final("字段名应为 unit_price，并按两位小数输出。"),
        ),
    ),
]
