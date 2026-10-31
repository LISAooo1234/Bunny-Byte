"""bench 任务集（六，单独的 long 套件）：大文件、多文件、一轮几十次调用的长回合。

八个约 300 行的模块，每个都在文件末尾自己格式化金额；需求是所有给用户看的金额都带千分位。
整份读完八个文件约 7 万字，会越过本轮历史的软上限、触发本轮压缩：用来看长回合里模型还能不能
看清刚读到的文件结尾、读文件去重会不会把它困在重读里、提示词前缀复用率在压缩之后还剩多少。
默认 `bunny bench` 不跑这一套，用 `--suite long` 选择。
"""

from .tasks import PY, BenchTask, final, tool

MODULES = (
    ("accounts", "Account balance"),
    ("invoices", "Invoice total"),
    ("payments", "Payment received"),
    ("refunds", "Refund issued"),
    ("taxes", "Tax due"),
    ("discounts", "Discount applied"),
    ("reminders", "Amount overdue"),
    ("exports", "Export total"),
)
RULES_PER_MODULE = 36


def _rule(module, index):
    field = f"{module}_field_{index}"
    return (
        f"def rule_{index}(record):\n"
        f'    """Business rule {index} for {module}: weight {field} by its tier."""\n'
        f'    value = record.get("{field}", 0)\n'
        f"    if value < 0:\n"
        f"        return 0\n"
        f"    tier = {index % 5 + 1}\n"
        f"    return value * tier + {index}\n"
    )


def module_source(module, label):
    rules = "\n\n".join(_rule(module, index) for index in range(RULES_PER_MODULE))
    return (
        f'"""{module.capitalize()} rules for the billing back office."""\n\n\n'
        f"{rules}\n\n"
        f"def score(record):\n"
        f"    return sum(rule(record) for rule in RULES)\n\n\n"
        f"RULES = [{', '.join(f'rule_{index}' for index in range(RULES_PER_MODULE))}]\n\n\n"
        f"def describe(amount):\n"
        f'    """The line shown to customers for this amount."""\n'
        f'    return "{label}: %.2f" % amount\n'
    )


FILES = {f"{module}.py": module_source(module, label) for module, label in MODULES}
FILES["test_money.py"] = '''import unittest

import invoices


class MoneyTest(unittest.TestCase):
    def test_invoice_total_has_thousands_separator(self):
        self.assertEqual(invoices.describe(1234.5), "Invoice total: 1,234.50")

    def test_small_invoice_total(self):
        self.assertEqual(invoices.describe(7), "Invoice total: 7.00")
'''
FILES["README.md"] = (
    "# billing\n\n每个模块（accounts、invoices、payments ……）的 `describe(amount)` 返回给客户看的那一行金额文字。\n"
)

VERIFY_MONEY_PY = "import importlib\n\nCASES = %r\n\nfor module, label in CASES:\n    mod = importlib.import_module(module)\n    got = mod.describe(1234567.891)\n    assert got == f\"{label}: 1,234,567.89\", (module, got)\n    assert mod.describe(5) == f\"{label}: 5.00\", (module, mod.describe(5))\n    assert mod.score({}) > 0\n" % (
    list(MODULES),
)


def _solution():
    steps = [tool("run_shell", command=f"{PY} -m unittest -q 2>&1")]
    steps += [tool("read_file", path=f"{module}.py") for module, _label in MODULES]
    for module, label in MODULES:
        steps.append(
            tool(
                "patch_file",
                path=f"{module}.py",
                old_text=f'    return "{label}: %.2f" % amount',
                new_text=f'    return f"{label}: {{amount:,.2f}}"',
            )
        )
    steps.append(tool("run_shell", command=f"{PY} -m unittest -q 2>&1"))
    steps.append(final("八个模块的 describe 都改成了千分位格式（1,234.50），测试通过。"))
    return tuple(steps)


LONG_TASKS = [
    BenchTask(
        id="long_context_ripple",
        category="长回合",
        title="大文件里分散在八个模块末尾的同一处格式要一起改",
        prompt=(
            "test_money.py 里 test_invoice_total_has_thousands_separator 失败了：给客户看的金额应该带千分位，"
            "比如 `1,234.50`。不只是 invoices，项目里每个模块的 describe 都要统一成这个格式。"
            "运行 `python3 -m unittest -q` 可以复现。请修复，不要修改测试文件。"
        ),
        files=FILES,
        hidden_files={"verify_money.py": VERIFY_MONEY_PY},
        frozen=("test_money.py",),
        verifier=f"{PY} -m unittest -q test_money && {PY} verify_money.py",
        step_budget=22,
        solution=_solution(),
    ),
]
