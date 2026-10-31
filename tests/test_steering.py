import threading

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.compact_support import fallback_summary_text
from bunnybyte.core.steering import PROMPT_HEADER, PROMPT_LIMIT
from bunnybyte.core.tool_loop_guard import current_turn_history
from bunnybyte.core.transcript_fence import collect_user_messages
from bunnybyte.testing import ScriptedModelClient


def build_agent(tmp_path, client):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return BunnyByte(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )


class SteeringClient(ScriptedModelClient):
    """Scripted model that lets the test queue a message while call N is "in flight"."""

    def __init__(self, outputs, steer_at):
        super().__init__(outputs)
        self.steer_at = dict(steer_at)
        self.agent = None

    def complete(self, prompt, max_new_tokens, **kwargs):
        call = len(self.prompts) + 1
        text = super().complete(prompt, max_new_tokens, **kwargs)
        if call in self.steer_at:
            self.agent.steer(self.steer_at[call])
        return text


def run(tmp_path, outputs, steer_at):
    client = SteeringClient(outputs, steer_at)
    agent = build_agent(tmp_path, client)
    client.agent = agent
    events = list(agent.engine.run_turn("写一个 hello.py"))
    return agent, client, events


def test_message_typed_during_a_tool_call_reaches_the_next_prompt_and_history(tmp_path):
    agent, client, events = run(
        tmp_path,
        ['<tool>{"name":"list_files","args":{}}</tool>', "<final>done</final>"],
        {1: "顺便用中文注释"},
    )

    assert len(client.prompts) == 2
    assert PROMPT_HEADER not in client.prompts[0]
    assert PROMPT_HEADER in client.prompts[1] and "- 顺便用中文注释" in client.prompts[1]
    steer_items = [item for item in agent.session["history"] if item.get("source") == "steer"]
    assert [item["content"] for item in steer_items] == ["顺便用中文注释"]
    assert steer_items[0]["turn_id"] == agent.session["history"][0]["turn_id"]
    delivered = [event for event in events if event["type"] == "steer_delivered"]
    assert delivered and delivered[0]["ids"] == [1]
    assert "用户追加：顺便用中文注释" in agent.current_task_state.user_request
    assert events[-1]["stop_reason"] == "final_answer_returned"


def test_message_arriving_with_the_final_answer_gets_one_more_model_call(tmp_path):
    agent, client, events = run(
        tmp_path,
        ["<final>写好了</final>", "<final>已改成 python3 shebang</final>"],
        {1: "加上 python3 shebang"},
    )

    assert len(client.prompts) == 2
    assert "加上 python3 shebang" in client.prompts[1]
    finals = [event["content"] for event in events if event["type"] == "final"]
    assert finals == ["已改成 python3 shebang"]
    preambles = [event["content"] for event in events if event["type"] == "assistant_preamble"]
    assert "写好了" in preambles  # 被插话打断的那次回答仍然显示、留在历史里
    roles = [(item["role"], item.get("source", "")) for item in agent.session["history"]]
    assert roles.index(("assistant", "runtime")) < roles.index(("user", "steer"))


def test_without_messages_the_prompt_is_unchanged(tmp_path):
    agent, client, _ = run(tmp_path, ["<final>ok</final>"], {})

    assert PROMPT_HEADER not in client.prompts[0]
    assert not agent.take_pending_steers()


def test_steer_messages_count_as_user_input_but_do_not_start_a_new_turn():
    history = [
        {"role": "user", "content": "task", "source": "user"},
        {"role": "tool", "name": "read_file", "content": "x"},
        {"role": "user", "content": "also do y", "source": "steer"},
        {"role": "tool", "name": "read_file", "content": "x"},
    ]

    assert len(current_turn_history(history)) == 3
    assert collect_user_messages(history) == ["task", "also do y"]
    summary = fallback_summary_text(history)
    assert "- Goal: task" in summary
    assert "also do y" in summary.split("- Goal:")[1]


def test_prompt_repeats_only_the_latest_messages(tmp_path):
    outputs = ['<tool>{"name":"list_files","args":{}}</tool>'] * (PROMPT_LIMIT + 2) + ["<final>ok</final>"]
    _, client, _ = run(tmp_path, outputs, {n: f"补充 {n}" for n in range(1, PROMPT_LIMIT + 3)})

    last = client.prompts[-1].split(PROMPT_HEADER, 1)[1]
    assert "补充 1\n" not in last and f"补充 {PROMPT_LIMIT + 2}" in last
    assert "earlier follow-up messages" in last


def test_stream_of_messages_at_final_time_cannot_loop_forever(tmp_path):
    outputs = [f"<final>答案 {n}</final>" for n in range(1, 60)]
    steer_at = {n: f"再改 {n}" for n in range(1, 60)}
    agent, client, events = run(tmp_path, outputs, steer_at)

    assert len(client.prompts) <= agent.max_steps + 2 + 5 + 1
    assert events[-1]["type"] == "turn_finished"


def test_queue_is_thread_safe_and_take_pending_returns_undelivered_text(tmp_path):
    agent = build_agent(tmp_path, ScriptedModelClient([]))
    threads = [threading.Thread(target=agent.steer, args=(f"m{n}",)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    pending = agent.take_pending_steers()
    assert sorted(text for _, text in pending) == sorted(f"m{n}" for n in range(20))
    assert len({steer_id for steer_id, _ in pending}) == 20
    assert agent.take_pending_steers() == []
    assert agent.steer("   ") == 0


def test_replay_timeline_shows_the_steer_message(tmp_path):
    from bunnybyte.replay_render import render_timeline

    agent, _, _ = run(
        tmp_path,
        ['<tool>{"name":"list_files","args":{}}</tool>', "<final>done</final>"],
        {1: "别动 README"},
    )
    trace = agent.current_run_dir / "trace.jsonl"
    import json

    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "» 用户插话 别动 README" in render_timeline(events)


def test_flood_of_messages_at_final_time_still_ends_with_a_real_answer(tmp_path):
    outputs = [f"<final>答案 {n}</final>" for n in range(1, 60)]
    agent, client, events = run(tmp_path, outputs, {n: f"再改 {n}" for n in range(1, 60)})

    assert len(client.prompts) == 6  # 1 + 5 次交卷时被插话
    assert events[-1]["stop_reason"] == "final_answer_returned"
    assert [event["content"] for event in events if event["type"] == "final"] == ["答案 6"]
    assert agent.take_pending_steers()  # 第 6 次之后的插话留给下一轮，不丢


def test_stop_requested_before_the_drain_leaves_the_message_queued(tmp_path):
    client = SteeringClient(['<tool>{"name":"list_files","args":{}}</tool>', "<final>x</final>"], {})
    agent = build_agent(tmp_path, client)
    client.agent = agent

    original = client.complete

    def complete_then_stop(prompt, max_new_tokens, **kwargs):
        text = original(prompt, max_new_tokens, **kwargs)
        agent.steer("停之前打的字")
        agent.abort_requested = True
        return text

    client.complete = complete_then_stop
    events = list(agent.engine.run_turn("go"))

    assert events[-1]["stop_reason"] == "aborted"
    assert not any(event["type"] == "steer_delivered" for event in events)
    assert [text for _, text in agent.take_pending_steers()] == ["停之前打的字"]


def test_a_stale_stop_request_does_not_abort_the_next_turn(tmp_path):
    agent, client, _ = run(tmp_path, ["<final>ok</final>"], {})
    agent.abort_requested = True  # 上一轮交卷之后才点的停止

    client.outputs = ["<final>第二轮</final>"]
    events = list(agent.engine.run_turn("再来"))

    assert events[-1]["stop_reason"] == "final_answer_returned"


def test_message_at_final_time_is_handled_before_the_verification_gate(tmp_path):
    from bunnybyte.core.verification import VerifyConfig

    client = SteeringClient(
        ['<tool>{"name":"write_file","args":{"path":"a.py","content":"x = 1\\n"}}</tool>', "<final>写好了</final>", "<final>按要求改完</final>"],
        {2: "变量改名 y"},
    )
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    agent = BunnyByte(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
        verify_config=VerifyConfig(mode="auto", commands=("true",), timeout=10, max_repair_rounds=1),
    )
    client.agent = agent
    events = list(agent.engine.run_turn("写 a.py"))

    kinds = [(event["type"], event.get("name", "")) for event in events]
    steer_index = kinds.index(("steer_delivered", ""))
    verify_indexes = [index for index, kind in enumerate(kinds) if kind == ("tool_call", "verify")]
    assert verify_indexes and all(index > steer_index for index in verify_indexes)  # 被插话打断的那次交卷没跑验证
    assert len(verify_indexes) == 1
