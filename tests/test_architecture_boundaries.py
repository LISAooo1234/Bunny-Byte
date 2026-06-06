from pathlib import Path


def test_core_modules_stay_below_entropy_budget():
    root = Path(__file__).resolve().parents[1]
    budgets = {
        "bunnybyte/core/runtime.py": 980,
        "bunnybyte/core/runtime_events.py": 90,
        "bunnybyte/core/runtime_consumers.py": 90,
        "bunnybyte/core/artifacts.py": 130,
        "bunnybyte/core/task_state.py": 140,
        "bunnybyte/core/todo_ledger.py": 120,
        "bunnybyte/core/worker_manager.py": 220,
        "bunnybyte/core/context_manager.py": 420,
        "bunnybyte/core/context_usage.py": 120,
        "bunnybyte/core/compact.py": 180,
        "bunnybyte/core/engine.py": 470,
        "bunnybyte/core/model_errors.py": 100,
        "bunnybyte/core/permissions.py": 140,
        "bunnybyte/core/tool_policy.py": 90,
        "bunnybyte/core/plan_mode.py": 140,
        "bunnybyte/core/tool_executor.py": 181,
        "bunnybyte/core/tool_profiles.py": 80,
        "bunnybyte/core/turn_history.py": 250,
        "bunnybyte/features/skills.py": 220,
        "bunnybyte/features/skills_bundled.py": 120,
        "bunnybyte/features/skills_runtime.py": 140,
        "bunnybyte/tools/registry.py": 360,
        "bunnybyte/tools/todos.py": 80,
        "bunnybyte/tools/agents.py": 90,
    }

    for relative_path, max_lines in budgets.items():
        line_count = len((root / relative_path).read_text(encoding="utf-8").splitlines())
        assert line_count <= max_lines, f"{relative_path} has {line_count} lines, budget is {max_lines}"
