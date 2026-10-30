import json

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.bench.runner import run_task
from bunnybyte.bench.tasks import select_tasks
from bunnybyte.config import resolve_project_critic_config
from bunnybyte.core.critic import CriticConfig, parse_review, resolve_critic_config
from bunnybyte.core.run_usage import trace_usage
from bunnybyte.headless import run_headless
from bunnybyte.providers.base import ModelResult
from bunnybyte.testing import ScriptedModelClient

ON = CriticConfig(mode="auto")
OBJECTION = json.dumps(
    {
        "verdict": "object",
        "objections": [
            {"file": "CHANGELOG.md", "issue": "没有写 CHANGELOG", "evidence": "AGENTS.md 要求每个修复都写一条，diff 里没有"}
        ],
    },
    ensure_ascii=False,
)
ACCEPT = '{"verdict": "accept"}'


def write_app(value):
    return f'<tool name="write_file" path="app.py"><content>VALUE = {value}\n</content></tool>'


def build_agent(tmp_path, outputs, critic=ON, **kwargs):
    (tmp_path / "AGENTS.md").write_text("- Every fix gets a CHANGELOG line.\n", encoding="utf-8")
    kwargs.setdefault("approval_policy", "auto")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        critic_config=critic,
        **kwargs,
    )


def critic_events(events, kind):
    return [event for event in events if event.get("name") == "critic" and event["type"] == kind]


def finals(events):
    return [event["content"] for event in events if event["type"] == "final"]


def test_objection_is_sent_back_and_the_repair_is_reviewed_again(tmp_path):
    agent = build_agent(
        tmp_path,
        [
            write_app(2),
            "<final>改好了。</final>",
            OBJECTION,
            '<tool name="write_file" path="CHANGELOG.md"><content>- fixed VALUE\n</content></tool>',
            "<final>改好了，也写了 CHANGELOG。</final>",
            ACCEPT,
        ],
        critic=CriticConfig(mode="auto", max_rounds=2),
    )

    events = list(agent.engine.run_turn("把 VALUE 改成 2"))

    assert finals(events) == ["改好了，也写了 CHANGELOG。"]
    results = critic_events(events, "tool_result")
    assert [item["metadata"]["tool_status"] for item in results] == ["error", "ok"]
    assert "CHANGELOG.md" in results[0]["content"] and results[1]["content"].startswith("审查通过")
    review_prompt = agent.model_client.prompts[2]
    assert "Every fix gets a CHANGELOG line" in review_prompt  # 项目规则
    assert "+VALUE = 2" in review_prompt  # 本轮 diff
    assert "* app.py" in review_prompt and "AGENTS.md" in review_prompt  # 文件列表，标出改动的文件
    repair_prompt = agent.model_client.prompts[3]
    assert "[tool:critic]" in repair_prompt and "没有写 CHANGELOG" in repair_prompt
    state = agent.current_task_state
    assert state.tool_steps == 2  # 审查本身不占工具步数，只有两次真实写文件
    assert (state.critic["rounds"], state.critic["objections"], state.critic["verdict"]) == (2, 1, "accept")
    report = json.loads((agent.current_run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["critic"]["rounds"] == 2
    trace = [json.loads(line) for line in agent.run_store.trace_path(state).read_text().splitlines()]
    assert [item["verdict"] for item in trace if item["event"] == "critic_review"] == ["object", "accept"]


def test_after_max_rounds_the_next_answer_is_accepted_without_another_review(tmp_path):
    agent = build_agent(tmp_path, [write_app(2), "<final>a</final>", OBJECTION, write_app(3), "<final>b</final>"])

    events = list(agent.engine.run_turn("改"))

    assert finals(events) == ["b"]
    assert len(critic_events(events, "tool_result")) == 1


def test_arguing_back_without_editing_is_not_reviewed_again(tmp_path):
    agent = build_agent(
        tmp_path,
        [write_app(2), "<final>a</final>", OBJECTION, "<final>这个仓库没有 CHANGELOG，意见不成立。</final>"],
        critic=CriticConfig(mode="auto", max_rounds=2),
    )

    events = list(agent.engine.run_turn("改"))

    assert finals(events) == ["这个仓库没有 CHANGELOG，意见不成立。"]
    assert len(critic_events(events, "tool_result")) == 1


@pytest.mark.parametrize(
    "reply, reason",
    [
        ("看起来不错！", "不是有效 JSON"),
        ('{"verdict": "object", "objections": [{"file": "app.py", "issue": "风格不好"}]}', "没有给出文件和证据"),
        ('{"verdict": "maybe"}', "无法识别"),
        (RuntimeError("provider 502"), "审查调用失败"),
    ],
)
def test_malformed_unsupported_or_failed_reviews_let_the_answer_through(tmp_path, reply, reason):
    agent = build_agent(tmp_path, [write_app(2), "<final>好了</final>", reply])

    events = list(agent.engine.run_turn("改"))

    assert finals(events) == ["好了"]
    assert agent.current_task_state.status == "completed"
    review = agent.current_task_state.critic["reviews"][0]
    assert review["verdict"] == "accept" and reason in review["reason"]
    assert critic_events(events, "tool_result")[0]["content"].startswith("审查未给出有效结论")


def test_fenced_or_chatty_json_is_still_understood():
    verdict, objections, _ = parse_review("好的，结论如下：\n```json\n" + OBJECTION + "\n```")
    assert verdict == "object" and objections[0]["file"] == "CHANGELOG.md"


def test_off_by_default_and_skipped_without_edits(tmp_path):
    agent = build_agent(tmp_path, [write_app(2), "<final>好了</final>"], critic=None)
    events = list(agent.engine.run_turn("改"))
    assert not critic_events(events, "tool_call")

    (tmp_path / "reader").mkdir()
    reader = build_agent(tmp_path / "reader", ["<final>只是看看</final>"])
    events = list(reader.engine.run_turn("解释一下"))
    assert not critic_events(events, "tool_call")


@pytest.mark.parametrize("flag", ["read_only", "plan", "worker"])
def test_critic_stays_out_of_read_only_plan_and_worker_runs(tmp_path, flag):
    kwargs = {"read_only": True} if flag == "read_only" else {"depth": 1} if flag == "worker" else {}
    agent = build_agent(tmp_path, [write_app(2), "<final>好了</final>"], **kwargs)
    if flag == "plan":
        agent.session["runtime_mode"] = {"mode": "plan"}
    from bunnybyte.core.critic_gate import critic_gate
    from bunnybyte.core.task_state import TaskState

    state = TaskState.create(task_id="t", user_request="改")
    state.changed_paths = ["app.py"]
    gate = critic_gate(agent.engine, state, "好了")
    with pytest.raises(StopIteration) as done:
        next(gate)
    assert done.value.value is False


def test_review_is_skipped_when_the_step_budget_is_almost_gone(tmp_path):
    agent = build_agent(tmp_path, [write_app(2), "<final>好了</final>"], max_steps=3)

    events = list(agent.engine.run_turn("改"))

    assert finals(events) == ["好了"]
    assert not critic_events(events, "tool_call")
    assert "步数" in agent.current_task_state.critic["skipped"]


def test_a_separate_critic_client_is_used_and_its_tokens_are_counted(tmp_path):
    agent = build_agent(tmp_path, [write_app(2), "<final>好了</final>"])
    agent.critic_client = ScriptedModelClient([ModelResult(text=ACCEPT, metadata={"input_tokens": 900, "output_tokens": 7})])

    list(agent.engine.run_turn("改"))

    assert len(agent.critic_client.prompts) == 1
    usage = trace_usage(agent.run_store.trace_path(agent.current_task_state))
    assert usage["input_tokens"] == 900 and usage["output_tokens"] == 7


def test_secret_values_in_the_diff_are_redacted_before_review(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_API_KEY", "sk-very-secret-value-123")
    agent = build_agent(
        tmp_path,
        ['<tool name="write_file" path="app.py"><content>KEY = "sk-very-secret-value-123"\n</content></tool>', "<final>好了</final>", ACCEPT],
        secret_env_names=["DEMO_API_KEY"],
    )

    list(agent.engine.run_turn("改"))

    assert "sk-very-secret-value-123" not in agent.model_client.prompts[2]


def test_headless_json_reports_the_critic(tmp_path):
    import io

    agent = build_agent(tmp_path, [write_app(2), "<final>好了</final>", OBJECTION, "<final>没有 CHANGELOG 文件，不需要。</final>"])
    out = io.StringIO()

    run_headless(agent, "改", output_format="json", stdout=out, stderr=io.StringIO())

    result = json.loads(out.getvalue())
    assert result["critic"]["rounds"] == 1 and result["critic"]["objections"] == 1
    assert result["critic"]["issues"][0]["file"] == "CHANGELOG.md"


def test_config_defaults_validation_and_cli_override(tmp_path):
    assert resolve_critic_config({}) == CriticConfig()
    assert resolve_critic_config({"critic": {"mode": "auto", "profile": "oneapi"}}).profile == "oneapi"
    with pytest.raises(ValueError):
        resolve_critic_config({"critic": {"mode": "ask"}})
    with pytest.raises(ValueError):
        resolve_critic_config({"critic": {"max_rounds": 9}})
    (tmp_path / ".bunnybyte.toml").write_text('[critic]\nmode = "auto"\nmax_rounds = 2\n', encoding="utf-8")
    assert resolve_project_critic_config(start=tmp_path).max_rounds == 2
    assert resolve_project_critic_config(start=tmp_path, mode="off").enabled is False


TESTS_ONLY_CHANGELOG = [
    '<tool>{"name":"read_file","args":{"path":"textstats.py"}}</tool>',
    '<tool name="patch_file" path="textstats.py"><old_text>text.split(" ")</old_text><new_text>text.split()</new_text></tool>',
    "<final>修好了，测试通过。</final>",
]
CHANGELOG_REPAIR = [
    '<tool>{"name":"read_file","args":{"path":"CHANGELOG.md"}}</tool>',
    '<tool name="patch_file" path="CHANGELOG.md"><old_text>## Unreleased\n</old_text>'
    "<new_text>## Unreleased\n- Fixed word_count on repeated spaces.\n</new_text></tool>",
    "<final>补上了 CHANGELOG。</final>",
]


def bench(task_id, outputs, **kwargs):
    return run_task(select_tasks([task_id])[0], ScriptedModelClient(outputs), timeout=60, **kwargs)


def test_bench_critic_catches_the_missing_changelog_and_the_repair_passes_grading():
    assert not bench("fix_and_update_changelog", TESTS_ONLY_CHANGELOG)["passed"]

    result = bench("fix_and_update_changelog", TESTS_ONLY_CHANGELOG[:-1] + [TESTS_ONLY_CHANGELOG[-1], OBJECTION, *CHANGELOG_REPAIR], critic=True)

    assert result["passed"], result["failure"]
    assert (result["critic_rounds"], result["critic_objections"]) == (1, 1)  # bench 只给 1 轮，修复后不再审
    assert result["objection_on_passing"] == 0  # 反对时代码确实还过不了判分
    assert result["critic_regression"] is False


def test_bench_counts_objections_raised_on_code_that_already_passed():
    task = select_tasks(["fix_and_update_changelog"])[0]
    full = TESTS_ONLY_CHANGELOG[:2] + CHANGELOG_REPAIR[:2] + ["<final>都做完了。</final>"]
    bogus = json.dumps({"verdict": "object", "objections": [{"file": "textstats.py", "issue": "x", "evidence": "y"}]})
    break_it = '<tool name="write_file" path="CHANGELOG.md"><content># Changelog\n</content></tool>'

    result = run_task(task, ScriptedModelClient(full + [bogus, break_it, "<final>按意见改了。</final>"]), timeout=60, critic=True)

    assert result["objection_on_passing"] == 1
    assert not result["passed"] and result["critic_regression"] is True


def test_checklist_items_marked_unmet_override_an_accept_verdict():
    reply = json.dumps(
        {
            "checklist": [
                {"requirement": "CHANGELOG line", "applies": True, "met": False, "evidence": "CHANGELOG.md is not in the diff"},
                {"requirement": "README usage", "applies": False, "met": False, "evidence": "no option changed"},
                {"requirement": "fix the test", "applies": True, "met": True, "evidence": "split()"},
            ],
            "verdict": "accept",
        }
    )

    verdict, objections, _ = parse_review(reply)

    assert verdict == "object"
    assert [item["issue"] for item in objections] == ["CHANGELOG line"]
    assert parse_review(json.dumps({"checklist": [{"requirement": "x", "applies": True, "met": True}], "verdict": "accept"}))[0] == "accept"


def test_tui_card_shows_the_review_model_and_the_verdict_line():
    from bunnybyte.tui.tool_cards import card_args_summary, summarize_tool_output, tool_title

    assert tool_title("critic") == "审查"
    assert card_args_summary("critic", {"model": "Claude Haiku 4.5"}) == "Claude Haiku 4.5"
    text = "审查提出 1 条意见，已退回给模型（2.3s）\n- CHANGELOG.md：没有写 CHANGELOG"
    assert summarize_tool_output("critic", text, {}) == "审查提出 1 条意见，已退回给模型（2.3s）"


def test_review_prompt_lists_the_commands_the_agent_ran_with_exit_codes(tmp_path):
    import shlex
    import sys

    command = f"{shlex.quote(sys.executable)} -c 'print(1)'"
    agent = build_agent(
        tmp_path,
        [write_app(2), "<tool>" + json.dumps({"name": "run_shell", "args": {"command": command}}) + "</tool>", "<final>好了</final>", ACCEPT],
    )

    list(agent.engine.run_turn("改"))

    review_prompt = agent.model_client.prompts[3]
    assert "agent ran `" in review_prompt and "-> exit 0" in review_prompt
    assert "never object because test output" in review_prompt


@pytest.mark.parametrize(
    "issue",
    ["Cannot verify the count is correct", "Cannot verify read_options() was left unchanged", "无法验证计数是否正确"],
)
def test_objections_that_only_say_the_reviewer_cannot_verify_are_dropped(issue):
    reply = json.dumps({"verdict": "object", "objections": [{"file": "a.py", "issue": issue, "evidence": "not shown"}]})

    verdict, objections, reason = parse_review(reply)

    assert (verdict, objections) == ("accept", [])
    assert "无法验证" in reason


def test_changed_and_root_files_are_listed_first_so_truncation_cannot_hide_them():
    from bunnybyte.core.critic import build_prompt

    files = [f"a/m{i:03}.py" for i in range(300)] + ["CHANGELOG.md", "z/changed.py"]
    prompt = build_prompt(request="r", rules="", files=files, changed=["z/changed.py"], diff="d", truncated=False, checks="", final="f")

    listing = prompt.split("## Files in the repository")[1].split("## Diff")[0]
    assert listing.index("* z/changed.py") < listing.index("CHANGELOG.md") < listing.index("a/m000.py")
    assert "and 102 more files" in listing


def test_no_review_call_once_an_abort_was_requested(tmp_path):
    agent = build_agent(tmp_path, [write_app(2), "<final>好了</final>"])
    from bunnybyte.core.critic_gate import critic_gate
    from bunnybyte.core.task_state import TaskState

    state = TaskState.create(task_id="t", user_request="改")
    state.changed_paths = ["app.py"]
    agent.abort_requested = True
    with pytest.raises(StopIteration) as done:
        next(critic_gate(agent.engine, state, "好了"))
    assert done.value.value is False
    assert agent.model_client.prompts == []


def test_critic_and_verify_gates_work_together(tmp_path):
    import shlex
    import sys

    from bunnybyte.core.verification import VerifyConfig

    (tmp_path / "check.py").write_text("import app\nassert app.VALUE == 2\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            write_app(2),
            "<final>好了</final>",
            OBJECTION,
            '<tool name="write_file" path="CHANGELOG.md"><content>- fixed\n</content></tool>',
            "<final>补了 CHANGELOG</final>",
        ],
        verify_config=VerifyConfig(mode="auto", commands=(f"{shlex.quote(sys.executable)} -B check.py",), timeout=30),
    )

    events = list(agent.engine.run_turn("改"))

    names = [event["name"] for event in events if event["type"] == "tool_result" and event.get("name") in {"verify", "critic"}]
    assert names == ["verify", "critic", "verify"]  # 审查后的修复会重新验证
    assert finals(events) == ["补了 CHANGELOG"]
    assert agent.current_task_state.verification["status"] == "passed"
