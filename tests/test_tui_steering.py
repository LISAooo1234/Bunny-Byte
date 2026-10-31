import threading

import pytest

from bunnybyte.core.steering import PROMPT_HEADER

from test_tui import build_agent, has_widget, wait_until


class GatedClient:
    """Each model call blocks until the test opens its gate, so the test can type while the turn runs."""

    provider = "test"
    protocol = "test"
    model = "gated"
    supports_native_tools = False
    supports_prompt_cache = False

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.gates = [threading.Event() for _ in outputs]
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens=None, **kwargs):
        index = len(self.prompts)
        self.prompts.append(prompt)
        self.gates[index].wait(timeout=10)
        return self.outputs[index]

    def abort(self):
        for gate in self.gates:
            gate.set()


def user_messages(app):
    from bunnybyte.tui.widgets import UserMessage

    return list(app.query(UserMessage))


async def start_turn(pilot, app, text="改一下 calc.py"):
    from bunnybyte.tui.widgets import InputBar

    bar = app.query_one(InputBar)
    bar.input.value = text
    await pilot.press("enter")
    await wait_until(pilot, lambda: len(app.agent.model_client.prompts) == 1)
    return bar


@pytest.mark.asyncio
async def test_text_typed_while_the_agent_works_is_queued_and_delivered_to_the_next_model_call(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp

    client = GatedClient(['<tool>{"name":"list_files","args":{}}</tool>', "<final>好了</final>"])
    agent = build_agent(tmp_path, [])
    agent.model_client = client
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = await start_turn(pilot, app)
        assert bar.input.disabled is False

        bar.input.value = "顺便加个类型注解"
        await pilot.press("enter")
        await pilot.pause(delay=0.05)

        steer_widget = user_messages(app)[-1]
        assert steer_widget.content == "顺便加个类型注解"
        assert steer_widget.steer_status == "queued"
        assert bar.input.value == ""

        client.gates[0].set()
        await wait_until(pilot, lambda: len(client.prompts) == 2)
        assert PROMPT_HEADER in client.prompts[1] and "顺便加个类型注解" in client.prompts[1]
        await wait_until(pilot, lambda: steer_widget.steer_status == "delivered")

        client.gates[1].set()
        await wait_until(pilot, lambda: not app._is_agent_turn_running())
        assert [item["content"] for item in agent.session["history"] if item.get("source") == "steer"] == ["顺便加个类型注解"]


@pytest.mark.asyncio
async def test_slash_commands_are_refused_while_the_agent_works(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp

    client = GatedClient(["<final>ok</final>"])
    agent = build_agent(tmp_path, [])
    agent.model_client = client
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = await start_turn(pilot, app)
        bar.input.value = "/clear"
        await pilot.press("enter")
        await pilot.pause(delay=0.05)

        assert bar.input.value == "/clear"  # 留在输入框里，等这一轮结束再发
        assert agent.take_pending_steers() == []
        client.gates[0].set()
        await wait_until(pilot, lambda: not app._is_agent_turn_running())


@pytest.mark.asyncio
async def test_approval_prompt_still_takes_y_while_the_input_accepts_steers(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import ConfirmPrompt

    client = GatedClient(['<tool>{"name":"write_file","args":{"path":"a.txt","content":"x"}}</tool>', "<final>ok</final>"])
    for gate in client.gates:
        gate.set()
    agent = build_agent(tmp_path, [], approval_policy="ask")
    agent.model_client = client
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = await start_turn(pilot, app, "写 a.txt")
        await wait_until(pilot, lambda: has_widget(app, ConfirmPrompt))
        assert bar.input.disabled is True
        await pilot.pause(delay=app.PROMPT_ARM_SECONDS + 0.1)  # 刚打过字时弹窗不接按键

        await pilot.press("y")
        await pilot.press("enter")
        await wait_until(pilot, lambda: not app._is_agent_turn_running())

        assert (tmp_path / "a.txt").read_text() == "x"
        assert bar.input.value == ""
        assert bar.input.disabled is False


@pytest.mark.asyncio
async def test_stop_puts_undelivered_messages_back_into_the_input(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp

    client = GatedClient(["<final>late</final>"])
    agent = build_agent(tmp_path, [])
    agent.model_client = client
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = await start_turn(pilot, app)
        bar.input.value = "其实别改测试"
        await pilot.press("enter")
        await pilot.pause(delay=0.05)

        await pilot.click("#stop-turn")
        await wait_until(pilot, lambda: not app._is_agent_turn_running())
        await wait_until(pilot, lambda: bar.input.value == "其实别改测试")

        assert len(client.prompts) == 1
        assert all(widget.content != "其实别改测试" for widget in user_messages(app))
        assert agent.take_pending_steers() == []


@pytest.mark.asyncio
async def test_messages_left_after_a_normal_finish_become_the_next_turn(tmp_path, monkeypatch):
    from bunnybyte.tui.app import BunnyByteTuiApp

    agent = build_agent(tmp_path, [])
    app = BunnyByteTuiApp(agent)
    sent = []

    async with app.run_test() as pilot:
        monkeypatch.setattr(app, "_run_agent", lambda text, mention_text=None: sent.append(text))
        agent.steer("第一条")
        agent.steer("第二条")
        app._last_stop_reason = "final_answer_returned"
        app._after_turn_steers()
        await pilot.pause(delay=0.05)

    assert sent == ["第一条\n\n第二条"]


@pytest.mark.asyncio
async def test_keys_typed_into_a_steer_draft_never_answer_an_approval_prompt(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import ConfirmPrompt

    client = GatedClient(['<tool>{"name":"write_file","args":{"path":"a.txt","content":"x"}}</tool>', "<final>ok</final>"])
    agent = build_agent(tmp_path, [], approval_policy="ask")
    agent.model_client = client
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        bar = await start_turn(pilot, app, "写 a.txt")
        await pilot.press("v", "e", "r", "i", "f", "y")  # 正在打一句补充要求，审批弹窗这时冒出来
        client.gates[0].set()
        await wait_until(pilot, lambda: has_widget(app, ConfirmPrompt))

        await pilot.press("y", "enter")  # 句子里的 y 和用来发送的回车
        await pilot.pause(delay=0.1)

        assert has_widget(app, ConfirmPrompt)  # 没被批准
        assert not (tmp_path / "a.txt").exists()
        assert [text for _, text in agent.steer_queue._items] == ["verifyy"]  # 草稿照常排队
        assert bar.input.value == "" and bar.input.disabled is True

        await pilot.press("y")  # 刚打完字：这一下也不算
        assert app._confirm_prompt.selected is False
        await pilot.pause(delay=app.PROMPT_ARM_SECONDS + 0.1)
        await pilot.press("n", "enter")
        client.gates[1].set()
        await wait_until(pilot, lambda: not app._is_agent_turn_running())
        assert not (tmp_path / "a.txt").exists()


@pytest.mark.asyncio
async def test_late_stop_after_a_normal_finish_puts_leftovers_back_instead_of_sending(tmp_path, monkeypatch):
    from bunnybyte.tui.app import BunnyByteTuiApp
    from bunnybyte.tui.widgets import InputBar

    agent = build_agent(tmp_path, [])
    app = BunnyByteTuiApp(agent)
    sent = []

    async with app.run_test() as pilot:
        monkeypatch.setattr(app, "_run_agent", lambda text, mention_text=None: sent.append(text))
        agent.steer("留着")
        app._last_stop_reason = "final_answer_returned"
        app._stop_requested = True  # 停止请求在引擎最后一次检查之后才到
        app._after_turn_steers()
        await pilot.pause(delay=0.05)

        assert sent == []
        assert app.query_one(InputBar).input.value == "留着"


@pytest.mark.asyncio
async def test_turn_finished_event_drives_the_next_turn_send(tmp_path):
    from bunnybyte.tui.app import BunnyByteTuiApp

    client = GatedClient(["<final>一</final>", "<final>二</final>"])
    agent = build_agent(tmp_path, [])
    agent.model_client = client
    app = BunnyByteTuiApp(agent)

    async with app.run_test() as pilot:
        await start_turn(pilot, app, "第一轮")
        client.gates[0].set()
        await wait_until(pilot, lambda: not app._is_agent_turn_running())
        assert app._last_stop_reason == "final_answer_returned"  # 来自引擎的 turn_finished 事件

        agent.steer("迟到的补充")  # 交卷之后才进队列：引擎已经取不到
        app._after_turn_steers()
        await wait_until(pilot, lambda: len(client.prompts) == 2)
        assert "迟到的补充" in client.prompts[1]
        client.gates[1].set()
        await wait_until(pilot, lambda: not app._is_agent_turn_running())
