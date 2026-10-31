import json

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.tool_loop_guard import (
    STALL_THRESHOLD,
    escalate_reply,
    clipped_continuation,
    prompt_view,
    read_content_visible,
    stall_notice,
)
from bunnybyte.testing import ScriptedModelClient

READ_ARGS = {"path": "a.txt", "start": 1, "end": 50}
STUB = "File range already read and unchanged: a.txt lines 1-3."


def build_agent(tmp_path, outputs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )


def tool_event(name, args, content):
    return {"role": "tool", "name": name, "args": args, "content": content}


def test_first_repeat_keeps_original_text_and_later_repeats_escalate():
    history = [{"role": "user", "content": "go"}]
    assert escalate_reply(STUB, history, "read_file", READ_ARGS) == STUB

    history.append(tool_event("read_file", READ_ARGS, STUB))
    second = escalate_reply(STUB, history, "read_file", READ_ARGS)
    assert second.startswith(STUB) and "2nd identical request" in second

    history.append(tool_event("read_file", READ_ARGS, second))
    third = escalate_reply(STUB, history, "read_file", READ_ARGS)
    assert "STOP" in third and "3 times" in third


def test_escalation_counts_only_the_current_turn():
    history = [
        {"role": "user", "content": "old"},
        tool_event("read_file", READ_ARGS, STUB),
        tool_event("read_file", READ_ARGS, STUB),
        {"role": "user", "content": "new"},
    ]
    assert escalate_reply(STUB, history, "read_file", READ_ARGS) == STUB


def test_stall_notice_needs_consecutive_wasted_replies():
    history = [{"role": "user", "content": "go"}]
    for _ in range(STALL_THRESHOLD - 1):
        history.append(tool_event("read_file", READ_ARGS, STUB))
    assert stall_notice(history) == ""

    history.append(tool_event("read_file", READ_ARGS, STUB))
    assert "final answer" in stall_notice(history)

    history.append(tool_event("run_shell", {"command": "ls"}, "exit_code: 0"))
    assert stall_notice(history) == ""


def read_delivery(path="a.txt", total=3, event_id="event_r1"):
    meta = (
        f'<read_file_meta path="{path}" start="1" end="{total}" '
        f'returned_lines="{total}" total_lines="{total}" eof="true" />'
    )
    item = tool_event("read_file", {"path": path, "start": 1, "end": 50}, f"# {path}\n   1: one\n   2: two\n   3: three\n{meta}")
    item["event_id"] = event_id
    return item


def test_read_is_redelivered_once_the_prompt_no_longer_shows_it():
    history = [{"role": "user", "content": "go"}, read_delivery()]
    # 还没渲染过提示词：按原样可见算
    assert read_content_visible(history, READ_ARGS) is True
    shown = {"visible_reads": {"event_r1": [1, 3, False, 3]}, "rendered_through": "event_r1"}
    assert read_content_visible(history, READ_ARGS, shown) is True

    collapsed = {"visible_reads": {}, "rendered_through": "event_r1"}
    assert read_content_visible(history, READ_ARGS, collapsed) is False

    # 上次提示词之后才到的结果，下一次提示词一定会带上
    history.append(read_delivery(event_id="event_r2"))
    assert read_content_visible(history, READ_ARGS, collapsed) is True


def test_clipped_result_is_visible_from_its_start_and_points_at_the_next_line():
    history = [{"role": "user", "content": "go"}, read_delivery()]
    clipped = {"visible_reads": {"event_r1": [1, 2, True, 3]}, "rendered_through": "event_r1"}

    # 同样的范围重新读还是会被截在第 2 行：不重发，提示从第 3 行接着读
    assert read_content_visible(history, READ_ARGS, clipped) is True
    assert clipped_continuation(history, READ_ARGS, clipped) == 3
    # 从第 3 行起的请求看不到：放行
    later = {"path": "a.txt", "start": 3, "end": 50}
    assert read_content_visible(history, later, clipped) is False
    assert clipped_continuation(history, later, clipped) is None


def test_read_content_visible_defaults_to_stub_without_any_delivery():
    assert read_content_visible([{"role": "user", "content": "go"}], READ_ARGS) is True


def test_agent_rereads_file_whose_earlier_result_was_compacted_away(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])
    agent.record({"role": "user", "content": "go"})
    first = agent.run_tool("read_file", READ_ARGS)
    agent.record(tool_event("read_file", READ_ARGS, first))
    assert agent.run_tool("read_file", READ_ARGS).startswith("File range already read")

    event_id = agent.session["history"][-1]["event_id"]
    agent.last_prompt_metadata = {"history": {"visible_reads": {}, "rendered_through": event_id}}
    assert prompt_view(agent) == {"visible_reads": {}, "rendered_through": event_id, "item_cap": None}
    reread = agent.run_tool("read_file", READ_ARGS)

    assert "two" in reread and not reread.startswith("File range already read")


def test_looping_model_gets_stall_notice_in_its_next_prompt(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    call = "<tool>" + json.dumps({"name": "read_file", "args": READ_ARGS}) + "</tool>"
    client = ScriptedModelClient([call] * (STALL_THRESHOLD + 2) + ["<final>done</final>"])
    agent = BunnyByte(
        model_client=client,
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
        max_steps=10,
    )

    assert agent.ask("inspect a.txt") == "done"

    prompts = [str(prompt) for prompt in getattr(client, "prompts", [])]
    assert any("Stop re-issuing them" in prompt for prompt in prompts)


def test_result_newer_than_the_last_prompt_uses_the_cap_that_prompt_rendered_with():
    body = "\n".join(f"{n:>4}: {'x' * 60}" for n in range(1, 301))
    meta = '<read_file_meta path="a.txt" start="1" end="300" returned_lines="300" total_lines="300" eof="true" />'
    history = [{"role": "user", "content": "go", "event_id": "event_u"}]
    WHOLE = {"path": "a.txt"}
    item = tool_event("read_file", WHOLE, f"# a.txt\n{body}\n{meta}")
    item["event_id"] = "event_new"
    history.append(item)

    # 上次提示词没压缩本轮（item_cap None）：新结果下一次整份可见，重读回答普通的“已读过”
    uncompacted = {"visible_reads": {}, "rendered_through": "event_u", "item_cap": None}
    assert read_content_visible(history, WHOLE, uncompacted) is True
    assert clipped_continuation(history, WHOLE, uncompacted) is None
    # 上次压缩时单条上限 4000 字：新结果会被同样截断，回答里给出接着读的行号
    compacted = {"visible_reads": {}, "rendered_through": "event_u", "item_cap": 4_000}
    assert read_content_visible(history, WHOLE, compacted) is True
    following = clipped_continuation(history, WHOLE, compacted)
    assert following is not None and 1 < following < 300
