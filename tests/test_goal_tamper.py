"""目标模式的测试篡改守卫：整个目标期间的改动对比开始时的快照，可疑时追加一轮让模型恢复测试。"""

import io
import json
import shlex
import sys

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte import cli
from bunnybyte.commands.goal_commands import handle_goal_command
from bunnybyte.core import goal_tamper
from bunnybyte.core.goal import ACHIEVED, PAUSED, UNMET, drive_goal, render_goal_status, start_goal, take_goal_drive
from bunnybyte.core.goal_review import GoalReviewConfig
from bunnybyte.core.goal_tamper import check_file_paths
from bunnybyte.core.verification import VerifyConfig
from bunnybyte.headless import EXIT_OK, EXIT_UNVERIFIED, prepare_agent, run_headless
from bunnybyte.replay_render import render_timeline
from bunnybyte.testing import ScriptedModelClient

pytest.importorskip("pytest")

PY = shlex.quote(sys.executable)
CHECK = f"{PY} -B -m pytest -q -p no:cacheprovider"
BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TESTS = """from calc import add


def test_add():
    assert add(2, 3) == 5


def test_add_zero():
    assert add(0, 0) == 0
"""
ONLY_ZERO = 'from calc import add\n\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n'
EXTRA = TESTS + "\n\ndef test_add_negative():\n    assert add(-1, -1) == -2\n"


def write(path, text):
    return f'<tool name="write_file" path="{path}"><content>{text}</content></tool>'


def read(path):
    return f'<tool name="read_file" path="{path}"></tool>'


DELETE_TEST = [read("test_calc.py"), write("test_calc.py", ONLY_ZERO)]  # 删掉会失败的 test_add，剩下的能过
RESTORE_AND_FIX = [read("test_calc.py"), write("test_calc.py", TESTS), read("calc.py"), write("calc.py", FIXED)]
TOUCH = [read("calc.py"), write("calc.py", "def add(a, b):\n    return a - b  # TODO\n")]


def make_agent(tmp_path, outputs, *, tamper="", resume=None, **kwargs):
    if resume is None:
        (tmp_path / "calc.py").write_text(BUGGY, encoding="utf-8")
        (tmp_path / "test_calc.py").write_text(TESTS, encoding="utf-8")
    store = SessionStore(tmp_path / ".bunnybyte" / "sessions")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=store,
        session=store.load(resume) if resume else None,
        verify_config=VerifyConfig(mode="off", max_repair_rounds=0, fix_proof="off", tamper=tamper),
        approval_policy="auto",
        auto_dream=False,
        **kwargs,
    )


def notices(events):
    return [event["content"] for event in events if event["type"] == "runtime_notice"]


def start(agent, **kwargs):
    goal, error = start_goal(agent, "让测试通过", check=CHECK, **kwargs)
    assert not error and goal["start_tree"]
    return goal


def test_deleting_the_failing_test_is_caught_and_the_extra_round_restores_it(tmp_path):
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>测试都过了。</final>", *RESTORE_AND_FIX, "<final>恢复了测试并修好 add。</final>"])
    start(agent)

    events = list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == ACHIEVED and len(goal["rounds"]) == 2
    assert goal["tamper_escalated"] and goal["tamper"]["status"] == "clean"
    assert any("追加一轮" in text and "删除了测试" in text for text in notices(events))
    assert "⚠" not in notices(events)[-1]
    prompt = agent.model_client.prompts[-1]
    assert "Restore these tests" in prompt and "test_add" in prompt
    assert "assert add(2, 3) == 5" in prompt  # 目标开始时的测试原文，非 git 仓库里模型只能靠它恢复
    assert (tmp_path / "test_calc.py").read_text(encoding="utf-8") == TESTS


def test_ignored_escalation_warns_but_achieves_in_warn_mode(tmp_path):
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>", *TOUCH, "<final>test_add 和目标矛盾。</final>"])
    start(agent)

    events = list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == ACHIEVED
    assert "⚠ 和目标开始时相比" in notices(events)[-1] and "test_add" in notices(events)[-1]


def test_ignored_escalation_is_unmet_in_block_mode_and_status_shows_it(tmp_path):
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>", *TOUCH, "<final>不改。</final>"], tamper="block")
    start(agent)

    list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == UNMET and "检查靠削弱测试才通过" in goal["reason"] and "追加一轮后仍未恢复" in goal["reason"]
    status = render_goal_status(agent)
    assert "测试篡改守卫" in status and "不算达成" in status and "test_add" in status


def test_failing_check_after_the_restore_round_stops_with_a_clear_reason(tmp_path):
    restore_only = [read("test_calc.py"), write("test_calc.py", TESTS)]
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>", *restore_only, "<final>test_add 要求 add(2,3)==5，做不到。</final>"])
    start(agent)

    list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == UNMET and goal["reason"].startswith("恢复测试后检查未通过")


def test_adding_tests_during_the_goal_is_not_a_warning(tmp_path):
    agent = make_agent(tmp_path, [read("calc.py"), write("calc.py", FIXED), read("test_calc.py"), write("test_calc.py", EXTRA), "<final>修好并加了测试。</final>"])
    start(agent)

    events = list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == ACHIEVED and goal["tamper"]["status"] == "clean"
    assert not goal.get("tamper_escalated") and "⚠" not in notices(events)[-1]


def test_a_test_added_then_deleted_within_the_goal_nets_out(tmp_path):
    outputs = [read("test_calc.py"), write("test_calc.py", EXTRA), "<final>先加个测试。</final>", read("calc.py"), write("calc.py", FIXED), read("test_calc.py"), write("test_calc.py", TESTS), "<final>修好了。</final>"]
    agent = make_agent(tmp_path, outputs)
    start(agent)

    list(drive_goal(agent))

    assert agent.session["goal"]["status"] == ACHIEVED and agent.session["goal"]["tamper"]["status"] == "clean"


def test_the_tamper_scan_runs_before_the_goal_evaluator(tmp_path):
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>", *RESTORE_AND_FIX, "<final>好</final>"])
    agent.goal_review_config = GoalReviewConfig(evaluator="main")
    agent.goal_review_client = ScriptedModelClient([json.dumps({"checklist": [], "verdict": "accept", "objections": []})])
    start(agent)

    list(drive_goal(agent))

    reviews = agent.session["goal"]["reviews"]
    assert [item["round"] for item in reviews] == [2]  # 第 1 轮的通过是删测试换来的：先追加一轮，不白花一次评审
    assert agent.session["goal"]["status"] == ACHIEVED


def test_escalation_is_skipped_when_the_budget_cannot_cover_another_round(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>"], tamper="block")
    start(agent, max_cost=1.0)
    real_run_turn = agent.engine.run_turn

    def expensive(message, mention_text=None):
        yield from real_run_turn(message, mention_text=mention_text)
        agent.session["cost"] = dict(agent.session.get("cost") or {}, usd=float(agent.session.get("cost", {}).get("usd") or 0) + 0.7)

    monkeypatch.setattr(agent.engine, "run_turn", expensive)

    list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == UNMET and "剩余预算不够" in goal["reason"]  # 不是 budget_limited：检查通过了，是测试被削弱
    assert not goal.get("tamper_escalated") and len(goal["rounds"]) == 1


def test_esc_during_the_restore_round_and_resume_resends_the_restore_message(tmp_path):
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>", read("calc.py")])
    start(agent)
    for event in drive_goal(agent):
        if event["type"] == "tool_result" and event["name"] == "read_file" and len(agent.session["goal"]["rounds"]) == 1:
            agent.abort_current_turn()
    assert agent.session["goal"]["status"] == PAUSED

    reloaded = make_agent(tmp_path, [*RESTORE_AND_FIX, "<final>好</final>"], resume=agent.session["id"])
    handle_goal_command(reloaded, "resume")
    assert take_goal_drive(reloaded)
    list(drive_goal(reloaded))

    assert "Restore these tests" in reloaded.model_client.prompts[0]
    assert reloaded.session["goal"]["status"] == ACHIEVED and reloaded.session["goal"]["tamper"]["status"] == "clean"


def test_last_round_gets_one_extra_restore_round(tmp_path):
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>", *RESTORE_AND_FIX, "<final>好</final>"])
    start(agent, max_rounds=1)

    events = list(drive_goal(agent))

    assert agent.session["goal"]["status"] == ACHIEVED
    assert "🎯 目标 追加一轮（恢复测试）" in notices(events)


def test_fixing_a_self_checking_source_named_in_the_check_is_not_tampering(tmp_path):
    (tmp_path / "selfcheck.py").write_text("def add(a, b):\n    return a - b\n\n\nassert add(2, 3) == 5\n", encoding="utf-8")
    fixed = "def add(a, b):\n    return a + b\n\n\nassert add(2, 3) == 5\n"
    agent = make_agent(tmp_path, [read("selfcheck.py"), write("selfcheck.py", fixed), "<final>修好了。</final>"], tamper="block")
    start_goal(agent, "修 add", check=f"{PY} -B selfcheck.py")

    events = list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == ACHIEVED and goal["tamper"]["status"] == "clean" and not goal.get("tamper_escalated")
    assert "⚠" not in notices(events)[-1]


def test_dropping_the_assert_from_a_check_script_is_flagged(tmp_path):
    (tmp_path / "selfcheck.py").write_text("def add(a, b):\n    return a - b\n\n\nassert add(2, 3) == 5\n", encoding="utf-8")
    gutted = "def add(a, b):\n    return a - b\n"
    agent = make_agent(tmp_path, [read("selfcheck.py"), write("selfcheck.py", gutted), "<final>好</final>", read("calc.py"), "<final>不改。</final>"], tamper="block")
    start_goal(agent, "修 add", check=f"{PY} -B selfcheck.py")

    list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == UNMET and goal["tamper"]["findings"][0]["kind"] == "check_file_changed"
    assert "assert add(2, 3) == 5" in agent.model_client.prompts[-1]  # 检查脚本的原文给了模型


def test_a_model_error_in_the_restore_round_keeps_the_restore_request_for_resume(tmp_path):
    from bunnybyte.providers.errors import ProviderError

    error = ProviderError("bad gateway", provider="openai", model="m", base_url="https://x.test/v1", code="server_error", http_status=502)
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>", read("calc.py"), error, error, error, error])
    start(agent)
    list(drive_goal(agent))
    goal = agent.session["goal"]
    assert goal["status"] == PAUSED and goal["rounds"][-1]["tamper_escalated"]  # 检查仍通过、测试仍缺：不能按 warn 直接达成
    assert len(agent.session["goal"]["rounds"]) == 2

    reloaded = make_agent(tmp_path, [*RESTORE_AND_FIX, "<final>好</final>"], resume=agent.session["id"])
    handle_goal_command(reloaded, "resume")
    take_goal_drive(reloaded)
    list(drive_goal(reloaded))

    assert "Restore these tests" in reloaded.model_client.prompts[0]
    assert reloaded.session["goal"]["status"] == ACHIEVED


def test_a_pass_in_a_model_error_round_pauses_instead_of_escalating(tmp_path):
    from bunnybyte.providers.errors import ProviderError

    error = ProviderError("bad gateway", provider="openai", model="m", base_url="https://x.test/v1", code="server_error", http_status=502)
    agent = make_agent(tmp_path, [*DELETE_TEST, error, error, error, error])
    start(agent)

    events = list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == PAUSED and "/goal resume" in goal["reason"] and len(goal["rounds"]) == 1
    assert goal["tamper_escalated"] and any("恢复测试的要求已记下" in text for text in notices(events))


def test_esc_while_the_guard_runs_pauses_and_resume_clears_the_old_verdict(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>"], tamper="block")
    start(agent)
    real = goal_tamper.scan_goal

    def scan_then_esc(agent_, goal):
        agent_.abort_requested = True
        return real(agent_, goal)

    monkeypatch.setattr(goal_tamper, "scan_goal", scan_then_esc)
    list(drive_goal(agent))

    goal = agent.session["goal"]
    assert goal["status"] == PAUSED and agent.abort_requested is False  # 不是 UNMET：用户按了停止
    handle_goal_command(agent, "resume")
    assert "tamper" not in agent.session["goal"]


def test_editing_a_check_script_named_in_the_command_is_flagged():
    goal = {"checks": [f"{PY} scripts/check.py && make test", "pytest tests/test_api.py"]}
    changed = [("M", "scripts/check.py"), ("M", "Makefile"), ("M", "tests/test_api.py"), ("A", "scripts/new.py"), ("M", "app.py")]
    assert check_file_paths(goal, changed) == ["scripts/check.py"]  # 测试文件交给 AST 扫描，新文件不算
    assert check_file_paths(goal, [("D", "Makefile")]) == []  # make 不点名 Makefile
    assert check_file_paths({"checks": ["make -f Makefile check"]}, [("D", "Makefile")]) == ["Makefile"]


def test_without_shadow_snapshots_the_scan_is_skipped_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("BUNNYBYTE_SHADOW", "off")
    monkeypatch.setattr(goal_tamper.ShadowStore, "for_root", classmethod(lambda cls, root: cls(root)))
    agent = make_agent(tmp_path, [read("calc.py"), write("calc.py", FIXED), "<final>好</final>"])
    goal, _ = start_goal(agent, "让测试通过", check=CHECK)
    assert goal["start_tree"] == ""

    events = list(drive_goal(agent))

    assert agent.session["goal"]["status"] == ACHIEVED and agent.session["goal"]["tamper"]["status"] == "skipped"
    assert "测试篡改检查跳过" in notices(events)[-1]


def test_off_mode_skips_the_goal_scan(tmp_path):
    agent = make_agent(tmp_path, [*DELETE_TEST, "<final>好</final>"], tamper="off")
    start(agent)

    list(drive_goal(agent))

    assert agent.session["goal"]["status"] == ACHIEVED and "tamper" not in agent.session["goal"]


def run_json(tmp_path, outputs, **kwargs):
    agent = make_agent(tmp_path, outputs)
    prepare_agent(agent)
    start(agent)
    out, err = io.StringIO(), io.StringIO()
    code = run_headless(agent, "让测试通过", output_format="json", stdout=out, stderr=err, goal=True, **kwargs)
    return agent, code, json.loads(out.getvalue()), err.getvalue()


def test_headless_goal_with_require_verified_exits_4_and_names_the_test(tmp_path):
    agent, code, result, err = run_json(tmp_path, [*DELETE_TEST, "<final>好</final>", *TOUCH, "<final>不改。</final>"], require_verified=True)

    assert code == EXIT_UNVERIFIED and result["goal"]["status"] == UNMET
    tamper = result["goal"]["tamper"]
    assert tamper["blocked"] and tamper["escalated"] and tamper["findings"][0]["kind"] == "removed_tests"
    assert "test_add" in err and "退出码 4" in err
    trace = [json.loads(line) for line in agent.run_store.trace_path(agent.current_task_state).read_text().splitlines()]
    assert "⚠ 目标篡改检查" in render_timeline(trace) and "不算达成" in render_timeline(trace)


def test_headless_goal_with_require_verified_and_a_real_fix_exits_0(tmp_path):
    _, code, result, _ = run_json(tmp_path, [read("calc.py"), write("calc.py", FIXED), "<final>好</final>"], require_verified=True)

    assert code == EXIT_OK and result["goal"]["tamper"]["status"] == "clean"


def test_cli_accepts_goal_with_require_verified(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    captured = {}
    from bunnybyte import headless

    monkeypatch.setattr(headless, "run_headless", lambda agent, prompt, **kwargs: captured.update(kwargs) or 0)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-dummy")
    code = cli.main(["-p", "让测试通过", "--goal", "--goal-check", "true", "--require-verified", "--approval", "auto", "--provider", "deepseek"])
    assert code == 0 and captured["goal"] and captured["require_verified"]
