from datetime import datetime, timezone

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.cost_meter import STOP_REASON_BUDGET_EXCEEDED, BudgetConfig, CostMeter, resolve_budget_config
from bunnybyte.core.critic import CriticConfig
from bunnybyte.core.pricing import BUILTIN_PRICES, call_cost, format_usd, is_deepseek_peak, lookup_price, normalize_model
from bunnybyte.providers.base import ModelResult
from bunnybyte.testing import ScriptedModelClient

OFF_PEAK = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)  # 周六
PEAK = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)  # 周五 UTC 02:00


def reply(text, model="Claude Haiku 4.5", protocol="anthropic", **tokens):
    tokens.setdefault("input_tokens", 1_000_000)
    tokens.setdefault("output_tokens", 0)
    return ModelResult(text=text, metadata={"provider_model": model, "provider_protocol": protocol, **tokens})


def write(path, text):
    return f'<tool name="write_file" path="{path}"><content>{text}\n</content></tool>'


def build_agent(tmp_path, outputs, limit=None, prices=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    kwargs.setdefault("approval_policy", "auto")
    agent = BunnyByte(
        model_client=ScriptedModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        auto_dream=False,
        **kwargs,
    )
    agent.cost_meter.configure(BudgetConfig(max_turn_cost=limit, prices=prices or {}))
    agent.cost_meter.clock = lambda: OFF_PEAK
    return agent


@pytest.mark.parametrize(
    "name, key",
    [
        ("Claude Haiku 4.5", "claude-haiku-4-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("anthropic/claude-sonnet-5.5", "claude-sonnet-5-5"),
        ("Claude Sonnet 5", "claude-sonnet-5"),
        ("deepseek-v4-flash", "deepseek-v4-flash"),
        ("GPT-5.4-mini", "gpt-5-4-mini"),
    ],
)
def test_model_names_from_gateways_and_dated_ids_normalize(name, key):
    assert normalize_model(name) == key
    assert lookup_price(name)[0] is BUILTIN_PRICES[key]


def test_sonnet_5_is_not_priced_as_sonnet_5_5_and_unknown_models_have_no_price():
    assert lookup_price("claude-sonnet-5")[0].cached_input == 0.2
    assert lookup_price("claude-sonnet-5-5")[0].cached_input == 0.1
    assert lookup_price("my-local-llama") == (None, "")


def test_anthropic_cache_reads_are_extra_while_openai_cached_tokens_are_a_subset():
    haiku = lookup_price("claude-haiku-4-5")[0]
    usage = {"input_tokens": 1_000_000, "cached_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert call_cost(haiku, usage, "anthropic") == pytest.approx(1 + 0.1 + 5)
    gpt = lookup_price("gpt-5.4")[0]
    assert call_cost(gpt, usage, "openai") == pytest.approx(0 + 0.25 + 15)
    writes = {"input_tokens": 0, "cache_creation_tokens": 1_000_000, "output_tokens": 0}
    assert call_cost(haiku, writes, "anthropic") == pytest.approx(1.25)


def test_haiku_5_5_long_prompt_tier_counts_cache_reads_toward_the_threshold():
    price = lookup_price("claude-haiku-5-5")[0]
    short = {"input_tokens": 60_000, "cached_tokens": 30_000}
    long = {"input_tokens": 60_000, "cached_tokens": 50_000}
    assert call_cost(price, short, "anthropic") == pytest.approx((60_000 * 0.1 + 30_000 * 0.01) / 1e6)
    assert call_cost(price, long, "anthropic") == pytest.approx((60_000 * 0.5 + 50_000 * 0.05) / 1e6)


def test_deepseek_peak_hours_double_the_price():
    assert is_deepseek_peak(PEAK) and not is_deepseek_peak(OFF_PEAK)
    assert not is_deepseek_peak(datetime(2026, 10, 30, 5, 0, tzinfo=timezone.utc))  # 两段高峰之间
    flash = lookup_price("deepseek-v4-flash")[0]
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert call_cost(flash, usage, "anthropic", OFF_PEAK) == pytest.approx(0.75)
    assert call_cost(flash, usage, "anthropic", PEAK) == pytest.approx(1.50)


def test_config_prices_override_and_bad_values_are_reported():
    config = resolve_budget_config(
        {
            "budget": {"max_turn_cost": 0.5},
            "pricing": {
                "My Model": {"input": 1, "output": 2},
                "claude-haiku-4-5": {"input": 0.5, "output": 2.5, "cached_input": 0.05},
                "broken": {"input": "cheap", "output": 1},
                "half": {"input": 1},
            },
        }
    )
    assert config.max_turn_cost == 0.5
    assert lookup_price("my-model", config.prices)[1] == "[pricing] 配置"
    assert lookup_price("Claude Haiku 4.5", config.prices)[0].input == 0.5
    assert any("broken" in item for item in config.problems) and any("half" in item for item in config.problems)
    assert resolve_budget_config({"budget": {"max_turn_cost": 0.5}}, max_cost=2.0).max_turn_cost == 2.0
    assert resolve_budget_config({"budget": {"max_turn_cost": -1}}).max_turn_cost is None


def test_format_usd_keeps_small_amounts_readable():
    assert [format_usd(v) for v in (0, 0.00042, 0.0123, 0.25, 1.5, None)] == ["$0", "$0.0004", "$0.0123", "$0.250", "$1.50", "-"]


def test_turn_and_session_costs_accumulate_and_survive_resume(tmp_path):
    agent = build_agent(
        tmp_path,
        [reply("<final>一</final>", input_tokens=100_000, output_tokens=10_000),
         reply("<final>二</final>", input_tokens=200_000, output_tokens=0)],
    )
    agent.ask("first")
    assert agent.cost_meter.turn["usd"] == pytest.approx(0.1 + 0.05)
    agent.ask("second")
    assert agent.cost_meter.turn["usd"] == pytest.approx(0.2)  # 每轮重新计
    assert agent.cost_meter.session_totals["usd"] == pytest.approx(0.35)
    entry = agent.cost_meter.session_totals["by_model"]["Claude Haiku 4.5"]
    assert (entry["calls"], entry["input_tokens"], entry["output_tokens"]) == (2, 300_000, 10_000)
    report = agent.build_report(agent.current_task_state)
    assert report["cost"]["usd"] == pytest.approx(0.2) and report["cost"]["estimate"] is True

    resumed = BunnyByte.from_session(
        model_client=ScriptedModelClient([reply("<final>三</final>", input_tokens=100_000)]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        session_id=agent.session["id"],
        approval_policy="auto",
        auto_dream=False,
    )
    resumed.ask("third")
    assert resumed.cost_meter.session_totals["usd"] == pytest.approx(0.45)


def test_budget_stop_ends_the_turn_before_the_next_model_call(tmp_path):
    outputs = [
        reply(write("a.txt", "one"), input_tokens=300_000),  # $0.30
        reply(write("b.txt", "two"), input_tokens=300_000),  # $0.60 >= $0.50
        reply("<final>不该被调用</final>"),
    ]
    agent = build_agent(tmp_path, outputs, limit=0.5)

    events = list(agent.engine.run_turn("写两个文件"))

    state = agent.current_task_state
    assert state.stop_reason == STOP_REASON_BUDGET_EXCEEDED and state.status == "stopped"
    assert len(agent.model_client.outputs) == 1  # 第三次调用没有发出
    assert (tmp_path / "b.txt").exists()  # 越过上限那次调用的工具照常执行，进度保留
    stop = [event["content"] for event in events if event["type"] == "stop"][0]
    assert "$0.600" in stop and "$0.50" in stop and "继续" in stop
    notices = [event["content"] for event in events if event["type"] == "runtime_notice"]
    assert any("80%" in text for text in notices) is False  # 0.3 < 0.4，0.6 直接越过上限，不发 80% 提示
    assert state.checkpoint_id
    assert agent.infer_next_step(state).startswith("Resume")


def test_eighty_percent_warning_is_sent_once(tmp_path):
    outputs = [
        reply(write("a.txt", "one"), input_tokens=850_000),
        reply(write("b.txt", "two"), input_tokens=10_000),
        reply("<final>好了</final>", input_tokens=10_000),
    ]
    agent = build_agent(tmp_path, outputs, limit=1.0)

    events = list(agent.engine.run_turn("写文件"))

    notices = [event["content"] for event in events if event["type"] == "runtime_notice" and "80%" in event["content"]]
    assert len(notices) == 1
    assert agent.current_task_state.status == "completed"


def test_cap_with_an_unpriced_model_warns_instead_of_silently_not_enforcing(tmp_path):
    agent = build_agent(tmp_path, [reply("<final>好</final>", model="my-local-llama")], limit=0.01)

    events = list(agent.engine.run_turn("hi"))

    notices = [event["content"] for event in events if event["type"] == "runtime_notice"]
    assert any("my-local-llama" in text and "[pricing" in text for text in notices)
    assert agent.cost_meter.turn["unpriced_calls"] == 1
    assert agent.current_task_state.status == "completed"


def test_fallback_served_calls_are_priced_by_the_model_that_answered():
    meter = CostMeter(clock=lambda: OFF_PEAK)
    metadata = {
        "provider_model": "Claude Sonnet 5.5",
        "provider_fallback": {"served_by": {"provider": "deepseek", "model": "deepseek-v4-flash"}},
        "provider_protocol": "anthropic",
        "input_tokens": 1_000_000,
        "output_tokens": 0,
    }
    assert meter.add(metadata) == pytest.approx(0.15)
    assert list(meter.turn["by_model"]) == ["deepseek-v4-flash"]


def test_calls_without_token_counts_are_not_counted():
    meter = CostMeter()
    assert meter.add({"provider_model": "claude-haiku-4-5"}) is None
    assert meter.turn["calls"] == 0


def test_critic_call_is_counted(tmp_path):
    critic_reply = reply('{"verdict": "accept", "objections": []}', input_tokens=100_000)
    agent = build_agent(
        tmp_path,
        [reply(write("app.py", "VALUE = 2"), input_tokens=100_000), reply("<final>好了</final>", input_tokens=100_000), critic_reply],
        critic_config=CriticConfig(mode="auto"),
    )
    agent.ask("改一下")
    turn = agent.cost_meter.turn
    assert turn["calls"] == 3 and turn["by_source"]["critic"] == pytest.approx(0.1)


def test_step_limit_summary_call_is_counted(tmp_path):
    limited = build_agent(
        tmp_path,
        [reply(write("x.txt", "1"), input_tokens=100_000), reply("<final>做到一半</final>", input_tokens=100_000)],
        max_steps=1,
    )
    limited.ask("做很多事")
    assert limited.current_task_state.stop_reason == "step_limit_reached"
    assert limited.cost_meter.turn["by_source"]["summary"] == pytest.approx(0.1)


def test_budget_stop_skips_the_paid_step_limit_summary(tmp_path):
    agent = build_agent(
        tmp_path,
        [reply(write("x.txt", "1"), input_tokens=1_000_000), reply("<final>总结</final>")],
        limit=0.5,
        max_steps=1,
    )
    agent.ask("做很多事")
    assert len(agent.model_client.outputs) == 1  # 总结调用没有发出


def test_worker_runtime_shares_the_parent_meter(tmp_path):
    from bunnybyte.core.worker_runtime import build_child_runtime

    agent = build_agent(tmp_path, [])
    child = build_child_runtime(agent, "Explore", [])
    assert child.cost_meter is agent.cost_meter
    child.cost_meter.add({"provider_model": "claude-haiku-4-5", "input_tokens": 1_000_000}, source="worker")
    assert agent.cost_meter.turn["by_source"]["worker"] == pytest.approx(1.0)
    child.cost_meter.start_turn(child)  # 子 agent 开始自己的回合不清零父 agent 的本轮计数
    assert agent.cost_meter.turn["usd"] == pytest.approx(1.0)


def test_usage_command_shows_cost_lines(tmp_path):
    from bunnybyte.cli import handle_repl_command

    agent = build_agent(tmp_path, [reply("<final>好</final>", input_tokens=10_000, output_tokens=1_000)], limit=1.0)
    agent.ask("hi")
    _, _, output = handle_repl_command(agent, "/usage")
    assert "| Cost this turn (estimate) | $0.0150（1 次调用） |" in output
    assert "| Turn cost limit | $1.00 |" in output
    assert "Cost · Claude Haiku 4.5" in output and "内置价目表" in output


def test_headless_json_reports_cost_and_exit_3_on_budget_stop(tmp_path):
    import io
    import json

    from bunnybyte.headless import EXIT_UNFINISHED, prepare_agent, run_headless

    agent = build_agent(tmp_path, [reply(write("a.txt", "1"), input_tokens=1_000_000), reply("<final>x</final>")], limit=0.5)
    prepare_agent(agent)
    out, err = io.StringIO(), io.StringIO()

    code = run_headless(agent, "写文件", output_format="json", stdout=out, stderr=err)

    result = json.loads(out.getvalue())
    assert code == EXIT_UNFINISHED and result["stop_reason"] == STOP_REASON_BUDGET_EXCEEDED
    assert result["cost"]["usd"] == pytest.approx(1.0) and result["cost"]["limit_usd"] == 0.5
    assert "退出码 3" in err.getvalue()


def test_cli_parses_max_cost_and_rejects_bad_values():
    from bunnybyte.cli import build_arg_parser

    parser = build_arg_parser()
    assert parser.parse_args(["--max-cost", "0.25"]).max_cost == 0.25
    with pytest.raises(SystemExit):
        parser.parse_args(["--max-cost", "-1"])


def test_status_bar_cost_segment(tmp_path):
    from bunnybyte.tui.status import cost_segment

    meter = CostMeter(clock=lambda: OFF_PEAK)
    assert cost_segment(meter) is None
    meter.limit = 1.0
    meter.add({"provider_model": "claude-haiku-4-5", "input_tokens": 100_000})
    assert cost_segment(meter).plain == "cost $0.100/$1.00 · 会话 $0"
    meter.bind_session({})
    meter.add({"provider_model": "mystery", "input_tokens": 1})
    assert cost_segment(meter).plain.endswith(" ?")


def test_bench_summary_reports_cost_per_pass_only_when_every_row_is_priced():
    from bunnybyte.bench.report import summarize

    rows = [
        {"passed": True, "cost_usd": 0.02, "steps": 1, "wall_seconds": 1.0},
        {"passed": False, "cost_usd": 0.04, "steps": 1, "wall_seconds": 1.0},
    ]
    summary = summarize(rows)
    assert summary["cost_usd"] == pytest.approx(0.06) and summary["cost_per_pass"] == pytest.approx(0.06)
    rows.append({"passed": True, "cost_usd": None, "steps": 1, "wall_seconds": 1.0})
    assert summarize(rows)["cost_usd"] is None and summarize(rows)["cost_per_pass"] is None


def test_project_config_budget_and_pricing_are_read(tmp_path, monkeypatch):
    from bunnybyte.config import resolve_project_budget_config

    monkeypatch.setattr("bunnybyte.config.DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")
    (tmp_path / ".bunnybyte.toml").write_text(
        '[budget]\nmax_turn_cost = 0.2\n\n[pricing."Gateway Model"]\ninput = 0.4\noutput = 1.6\n', encoding="utf-8"
    )
    config = resolve_project_budget_config(start=tmp_path)
    assert config.max_turn_cost == 0.2 and lookup_price("gateway-model", config.prices)[0].output == 1.6
    assert resolve_project_budget_config(start=tmp_path, max_cost=1.0).max_turn_cost == 1.0


def test_anthropic_usage_keeps_cache_write_tokens():
    from bunnybyte.providers.clients import _extract_anthropic_usage_details

    details = _extract_anthropic_usage_details(
        {"usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 7}}
    )
    assert (details["cached_tokens"], details["cache_creation_tokens"]) == (50, 7)


def test_background_runs_count_toward_the_session_but_not_the_turn_budget(tmp_path):
    from bunnybyte.features.memory import run_dream

    agent = build_agent(
        tmp_path,
        [reply("<final>好</final>", input_tokens=1_010_000), reply("<final>记忆整理完了</final>", input_tokens=100_000)],
        limit=0.5,
    )
    agent.ask("写文件")
    assert agent.cost_meter.over_budget()  # 本轮的那次调用越过了上限

    result = run_dream(agent)

    assert "记忆整理完了" in str(result)  # 记忆整理没有被用户这一轮的预算拦下
    assert agent.cost_meter.turn["usd"] == pytest.approx(1.01)  # 本轮不变
    assert agent.cost_meter.session_totals["usd"] == pytest.approx(1.11)  # 会话累计算进去了
    meter = agent.cost_meter.background(agent)
    assert meter.limit is None and meter.turn is not agent.cost_meter.turn


class _UnscriptedClient:
    """没有 outputs 属性的替身：真实 client 才会触发回合索引和技能演化的模型调用。"""

    model = "claude-haiku-4-5"

    def __init__(self, texts):
        self.texts = list(texts)
        self.last_completion_metadata = {}

    def complete_result(self, prompt, max_new_tokens, **kwargs):
        return ModelResult(text=self.texts.pop(0), metadata={"provider_model": self.model, "input_tokens": 100_000})


def test_turn_index_and_compaction_calls_are_counted(tmp_path):
    agent = build_agent(tmp_path, [])
    agent.model_client = _UnscriptedClient(['<final>好</final>', '{"keywords": ["x"], "summary": "s"}'])
    agent.ask("hi")
    assert agent.cost_meter.turn["by_source"]["turn_index"] == pytest.approx(0.1)

    from bunnybyte.core.compact_support import model_summary_text

    agent.model_client = _UnscriptedClient(["<summary>压缩</summary>"])
    model_summary_text(agent, [{"role": "user", "content": "old"}], lambda items: [("t1", items)])
    assert agent.cost_meter.session_totals["by_source"]["compaction"] == pytest.approx(0.1)


def test_bench_rows_carry_cost_and_use_configured_prices(tmp_path):
    from bunnybyte.bench.runner import run_task
    from bunnybyte.bench.tasks import all_tasks
    from bunnybyte.core.pricing import Price

    task = all_tasks()[0]
    client = ScriptedModelClient([reply("<final>不会过</final>", model="gateway-x", input_tokens=1_000_000)])
    row = run_task(task, client, timeout=60, prices={"gateway-x": Price(2, 4)})
    assert row["cost_usd"] == pytest.approx(2.0)
    unpriced = run_task(task, ScriptedModelClient([reply("<final>x</final>", model="gateway-x")]), timeout=60)
    assert unpriced["cost_usd"] is None


def test_status_bar_shows_the_cost_segment_after_a_turn(tmp_path):
    from bunnybyte.tui.status import StatusBar

    agent = build_agent(tmp_path, [reply("<final>好</final>", input_tokens=100_000)], limit=1.0)
    agent.ask("hi")
    bar = StatusBar()
    bar.update_agent(agent)
    assert any(text.plain.startswith("cost $0.100/$1.00") for _, text in bar.segments())


def test_fork_starts_its_cost_at_zero(tmp_path):
    agent = build_agent(tmp_path, [reply("<final>一</final>", input_tokens=100_000)])
    agent.ask("first")
    agent.fork_session("latest")
    assert agent.cost_meter.session_totals["usd"] == 0


def test_bedrock_global_ids_match():
    assert normalize_model("global.anthropic.claude-sonnet-4-5-20250929-v1:0") == "claude-sonnet-4-5"
