"""长回合里刚读到的内容必须完整可见，压缩切点只成批前进（提示词前缀缓存不必每次都断）。"""

import json
import os
import subprocess

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.turn_history_support import ITEM_CHAR_CAP, clip_read_result, verbatim_cut
from bunnybyte.testing import ScriptedModelClient


def call(name, **args):
    return "<tool>" + json.dumps({"name": name, "args": args}) + "</tool>"


def module(index, functions=60):
    body = "".join(
        f"def f{index}_{j}(x):\n    return x + {j}  # helper number {j} in module {index}\n" for j in range(functions)
    )
    return body + f"\nMARKER_END_{index} = True\n"


def repo(tmp_path, count=20, functions=60):
    for index in range(count):
        (tmp_path / f"mod{index}.py").write_text(module(index, functions), encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def agent_for(root, outputs, sessions="sessions"):
    client = ScriptedModelClient(outputs)
    agent = BunnyByte(
        model_client=client,
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".bunnybyte" / sessions),
        approval_policy="auto",
        max_steps=80,
    )
    return agent, client


def reuse(prompts):
    return [len(os.path.commonprefix([a, b])) / len(b) for a, b in zip(prompts, prompts[1:])]


def last_tool_reply(agent):
    return str([item for item in agent.session["history"] if item.get("role") == "tool"][-1]["content"])


def test_every_read_stays_fully_visible_in_the_next_prompt_of_a_long_turn(tmp_path):
    root = repo(tmp_path)
    agent, client = agent_for(root, [call("read_file", path=f"mod{i}.py") for i in range(20)] + ["<final>ok</final>"])
    agent.ask("read all modules")

    # 以前从第 11 次调用起，刚读的文件只剩前 900 字（看不到结尾）
    for index, prompt in enumerate(client.prompts[1:]):
        assert f"MARKER_END_{index} = True" in prompt, index
    assert agent.last_prompt_metadata["history"]["same_turn_compacted_tools"] > 0


def test_compaction_moves_in_batches_so_most_calls_reuse_the_previous_prompt(tmp_path):
    root = repo(tmp_path)
    agent, client = agent_for(root, [call("read_file", path=f"mod{i}.py") for i in range(20)] + ["<final>ok</final>"])
    agent.ask("read all modules")

    shares = reuse(client.prompts)
    breaks = [index for index, share in enumerate(shares) if share < 0.6]
    # 滑动窗口时第 11 次起每次都只复用 66%；成批切之后 20 次调用里只断两次
    assert len(breaks) <= 3, shares
    history_tails = [prompt.index("\n\nRelevant memory:") for prompt in client.prompts]
    for index, (previous, current) in enumerate(zip(client.prompts, client.prompts[1:])):
        if index in breaks:
            continue
        assert current.startswith(previous[: history_tails[index]]), index


def test_cut_only_moves_forward_when_items_are_appended():
    sizes = [5_000] * 30
    cuts = [verbatim_cut(sizes[:n], 0, 50_000) for n in range(1, 31)]
    assert cuts == sorted(cuts)
    assert len(set(cuts)) - 1 <= 4  # 30 个结果只切 4 次，每次推进 6 条
    assert all(sum(sizes[cut:n]) <= 50_000 for cut, n in zip(cuts, range(1, 31)))


def test_reread_of_a_collapsed_result_is_delivered_again(tmp_path):
    root = repo(tmp_path, count=14)
    outputs = [call("read_file", path=f"mod{i}.py") for i in range(13)] + [call("read_file", path="mod0.py"), "<final>ok</final>"]
    agent, client = agent_for(root, outputs)
    agent.ask("go")

    assert last_tool_reply(agent).startswith("# mod0.py")
    assert "MARKER_END_0 = True" in client.prompts[-1]


def test_reread_in_a_short_turn_after_long_history_is_not_refused(tmp_path):
    # 整段历史超预算后进入压缩渲染；新的一轮只有两次读，以前也会把结果截到 900 字却拒绝重读
    root = repo(tmp_path, count=24)
    agent, client = agent_for(root, [])
    for turn in range(4):
        client.outputs.extend([call("read_file", path=f"mod{turn * 5 + i}.py") for i in range(5)] + ["<final>ok</final>"])
        agent.ask(f"turn {turn}")
    client.outputs.extend([call("read_file", path="mod20.py"), call("read_file", path="mod20.py"), "<final>ok</final>"])
    agent.ask("look at mod20")

    assert "MARKER_END_20 = True" in client.prompts[-2]
    assert last_tool_reply(agent).startswith("File range already read")


def test_big_file_is_clipped_at_a_line_with_a_continue_hint_and_the_reread_does_not_loop(tmp_path):
    root = repo(tmp_path, count=12)
    (root / "big.py").write_text(module(99, functions=600), encoding="utf-8")
    outputs = [call("read_file", path=f"mod{i}.py") for i in range(12)]
    outputs += [call("read_file", path="big.py"), call("read_file", path="big.py"), "<final>ok</final>"]
    agent, client = agent_for(root, outputs)
    agent.ask("go")

    prompt = client.prompts[-2]
    shown = prompt[prompt.index('[tool:read_file] {"path": "big.py"}') :]
    assert "...[showing lines 1-" in shown and "call read_file with start=" in shown
    assert "MARKER_END_99" not in shown
    start = int(shown.split("call read_file with start=", 1)[1].split()[0].rstrip("]"))
    # 同一范围再读也会被截在同一行：不重发整份文件，而是告诉模型从哪一行接着读
    reply = last_tool_reply(agent)
    assert reply.startswith("File range already read") and f"start={start}" in reply


def test_reading_past_the_clipped_part_is_allowed(tmp_path):
    root = repo(tmp_path, count=12)
    (root / "big.py").write_text(module(99, functions=600), encoding="utf-8")
    outputs = [call("read_file", path=f"mod{i}.py") for i in range(12)]
    outputs += [call("read_file", path="big.py"), call("read_file", path="big.py", start=1100, end=1300), "<final>ok</final>"]
    agent, client = agent_for(root, outputs)
    agent.ask("go")

    assert "MARKER_END_99 = True" in last_tool_reply(agent)


def test_clip_read_result_keeps_whole_lines_and_reports_the_shown_range():
    content = "# a.py\n" + "\n".join(f"{n:>4}: {'x' * 60}" for n in range(1, 501)) + (
        '\n<read_file_meta path="a.py" start="1" end="500" returned_lines="500" total_lines="500" eof="true" />'
    )
    text, shown = clip_read_result(content, 4_000)
    assert len(text) <= 4_000
    assert shown[0] == 1 and shown[2] is True and shown[3] == 500
    assert f"{shown[1]:>4}: " in text and f"{shown[1] + 1:>4}: " not in text
    assert clip_read_result(content, None)[1] == [1, 500, False, 500]
    assert ITEM_CHAR_CAP >= 8_000


def test_reread_with_another_spelling_of_the_path_is_delivered_after_collapse(tmp_path):
    root = repo(tmp_path, count=14)
    outputs = [call("read_file", path=f"mod{i}.py") for i in range(13)] + [call("read_file", path="./mod0.py"), "<final>ok</final>"]
    agent, client = agent_for(root, outputs)
    agent.ask("go")

    # 账本按规范路径记，历史里是 mod0.py：以前 ./mod0.py 会被拒绝，可内容早已不在提示词里
    assert last_tool_reply(agent).startswith("# mod0.py")


def test_two_reads_of_a_big_file_in_a_short_turn_are_not_reported_as_clipped(tmp_path):
    root = repo(tmp_path, count=1)
    (root / "big.py").write_text(module(99, functions=400), encoding="utf-8")
    outputs = [call("read_file", path="big.py"), call("read_file", path="big.py"), "<final>ok</final>"]
    agent, client = agent_for(root, outputs)
    agent.ask("go")

    # 本轮没压缩：整份文件下一次提示词里完整可见，重读回答“已读过”而不是“从第 N 行接着读”
    assert "MARKER_END_99 = True" in client.prompts[-1]
    reply = last_tool_reply(agent)
    assert reply.startswith("File range already read") and "clipped" not in reply


def test_forced_path_keeps_the_tail_read_visible_and_the_cut_stable_under_a_small_budget(tmp_path):
    from bunnybyte.core.context_manager import ContextManager

    root = repo(tmp_path, count=24)
    agent, client = agent_for(root, [])
    for turn in range(3):
        client.outputs.extend([call("read_file", path=f"mod{turn * 6 + i}.py") for i in range(6)] + ["<final>ok</final>"])
        agent.ask(f"turn {turn}")
    client.outputs.extend([call("read_file", path=f"mod{18 + i}.py") for i in range(6)] + ["<final>ok</final>"])
    manager = ContextManager(agent, total_budget=40_000)
    agent.context_manager = manager
    agent.ask("turn 3")

    history = agent.last_prompt_metadata["history"]
    assert history["raw_chars"] > manager.section_budgets["history"]  # 走的是整段超预算的强制压缩
    assert history["budget_clipped"] is False  # 本轮原样部分只用前几轮剩下的空间，开头不被截
    # 预算很小：最后一次读按单条上限按整行截断，看得到的范围记在 visible_reads 里，并带着从哪一行接着读
    last_read = [item for item in agent.session["history"] if item.get("name") == "read_file"][-1]
    assert last_read["args"]["path"] == "mod23.py"
    shown = history["visible_reads"][last_read["event_id"]]
    assert f"{shown[1]:>4}: " in client.prompts[-1] and f"call read_file with start={shown[1] + 1}" in client.prompts[-1]
    # 同一张历史，预算临时缩小时切点不动（按配置的预算算）：本轮渲染出的部分逐字相同
    renderer = agent.context_manager.history_builder
    full_budget = manager.section_budgets["history"]
    normal, _ = renderer.render_section(full_budget, stable_budget=full_budget)
    reduced, _ = renderer.render_section(full_budget - 500, stable_budget=full_budget)
    turn_header = normal.rindex("Turn ")
    assert normal[turn_header:] == reduced[reduced.rindex("Turn ") :]


def test_front_clip_keeps_tail_reads_visible():
    from bunnybyte.core.turn_history_support import TurnHistoryRenderer

    class Stub:
        memory = None

    def read_item(index, turn, lines=120):
        body = "\n".join(f"{n:>4}: line {n} of file {index}" for n in range(1, lines + 1))
        meta = f'<read_file_meta path="f{index}.py" start="1" end="{lines}" returned_lines="{lines}" total_lines="{lines}" eof="true" />'
        return {"role": "tool", "name": "read_file", "args": {"path": f"f{index}.py"}, "content": f"# f{index}.py\n{body}\n{meta}", "turn_id": turn, "event_id": f"e{index}"}

    # 前面几轮自己就超过预算：本轮原样部分只剩下限 4000 字，整段仍会从开头被截
    history = []
    for turn in range(3):
        history.append({"role": "user", "content": f"turn {turn}", "turn_id": f"t{turn}", "event_id": f"u{turn}"})
        history += [{"role": "assistant", "content": "x" * 900, "turn_id": f"t{turn}", "event_id": f"a{turn}_{n}"} for n in range(5)]
    history.append({"role": "user", "content": "go", "turn_id": "t9", "event_id": "u9"})
    history += [read_item(i, "t9", lines=30) for i in range(12)]
    text, details = TurnHistoryRenderer(Stub()).render_section(history, 9_000, stable_budget=9_000)
    assert details["budget_clipped"] is True
    visible = details["visible_reads"]
    assert visible, "截掉开头之后，末尾完整的读结果仍然算可见"
    for event_id in visible:
        index = int(event_id[1:])
        assert f"  30: line 30 of file {index}" in text


def test_a_reduced_history_budget_does_not_move_the_current_turn_cut(tmp_path):
    from bunnybyte.core.context_manager import ContextManager

    root = repo(tmp_path)
    agent, client = agent_for(root, [call("read_file", path=f"mod{i}.py") for i in range(14)] + ["<final>ok</final>"])
    agent.ask("read")
    manager = ContextManager(agent)
    budget = manager.section_budgets["history"]
    # 超预算时 history 节会被临时缩小；切点仍按配置的预算算，渲染结果逐字不变
    assert manager._render_history_section(budget).rendered == manager._render_history_section(budget - 15_000).rendered
