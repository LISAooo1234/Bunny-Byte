import json

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.tool_loop_guard import (
    STALL_THRESHOLD,
    escalate_reply,
    read_content_visible,
    same_turn_compacted,
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


def read_delivery(path="a.txt", total=3):
    meta = (
        f'<read_file_meta path="{path}" start="1" end="{total}" '
        f'returned_lines="{total}" total_lines="{total}" eof="true" />'
    )
    return tool_event("read_file", {"path": path, "start": 1, "end": 50}, meta + "\none\ntwo\nthree")


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


def test_read_is_redelivered_once_earlier_content_scrolled_out_of_the_prompt():
    history = [{"role": "user", "content": "go"}, read_delivery()]
    assert read_content_visible(history, READ_ARGS) is True

    for index in range(4):
        history.append(tool_event("list_files", {"path": f"d{index}"}, "(empty)"))
    assert read_content_visible(history, READ_ARGS) is False

    history.append(read_delivery())
    assert read_content_visible(history, READ_ARGS) is True


def test_old_read_stays_visible_while_the_turn_is_not_compacted():
    history = [{"role": "user", "content": "go"}, read_delivery()]
    for index in range(6):
        history.append(tool_event("list_files", {"path": f"d{index}"}, "(empty)"))

    assert read_content_visible(history, READ_ARGS, compacted=False) is True
    assert read_content_visible(history, READ_ARGS, compacted=True) is False


def test_read_content_visible_defaults_to_stub_without_any_delivery():
    assert read_content_visible([{"role": "user", "content": "go"}], READ_ARGS) is True


def test_agent_rereads_file_whose_earlier_result_was_compacted_away(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    agent = build_agent(tmp_path, [])
    agent.record({"role": "user", "content": "go"})
    first = agent.run_tool("read_file", READ_ARGS)
    agent.record(tool_event("read_file", READ_ARGS, first))
    assert agent.run_tool("read_file", READ_ARGS).startswith("File range already read")

    for index in range(4):
        agent.record(tool_event("list_files", {"path": f"d{index}"}, "(empty)"))
    assert agent.run_tool("read_file", READ_ARGS).startswith("File range already read")

    agent.last_prompt_metadata = {"context_usage": {"history": {"same_turn_compacted_tools": 3}}}
    assert same_turn_compacted(agent) is True
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
