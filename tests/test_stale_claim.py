"""模型把本回合自己做的工作说成“之前的会话已经做完了”（Haiku 4.5 核心 bench 30 行里 11 行）。

提示词里本回合只剩一串工具结果、没有模型自己说的话，空的“历史召回”还写着“之前会话的证据”，
模型下一次调用就把这些当成前情。现在：原生工具调用前那句话记进历史，规则里写明最后一个 Turn 就是
本回合，召回没有命中时不再写那句。bench 每行记 stale_claim。
"""

import json
import subprocess
from pathlib import Path

import pytest

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.bench import report
from bunnybyte.bench.stale_claim import stale_claim
from bunnybyte.core.transcript_recall import render_prompt_recall
from bunnybyte.providers.base import ModelResult, ModelToolCall
from bunnybyte.testing import ScriptedModelClient


def repo(tmp_path):
    (tmp_path / "calc.py").write_text("def mean(xs):\n    return sum(xs) // len(xs)\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return tmp_path


def build(tmp_path, outputs, native=True):
    return BunnyByte(
        model_client=ScriptedModelClient(outputs, supports_native_tools=native, native_tool_protocol="anthropic"),
        workspace=WorkspaceContext.build(repo(tmp_path)),
        session_store=SessionStore(tmp_path / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )


def native(text, name, **args):
    return ModelResult(text=text, tool_calls=[ModelToolCall(id=f"call_{name}", name=name, args=args)])


def transcript(prompt):
    return prompt[prompt.index("Transcript:") : prompt.index("Runtime state")]


def test_text_before_a_native_tool_call_stays_in_the_transcript(tmp_path):
    agent = build(
        tmp_path,
        [
            native("I'll read calc.py first.", "read_file", path="calc.py"),
            native("", "patch_file", path="calc.py", old_text="//", new_text="/"),
            "Fixed mean() to use true division.",
        ],
    )

    assert agent.ask("fix mean") == "Fixed mean() to use true division."

    last = transcript(agent.model_client.prompts[-1])
    assert "[assistant] I'll read calc.py first." in last
    assert last.index("[user] fix mean") < last.index("[assistant] I'll read") < last.index("[tool:read_file]")
    assert last.count("[assistant]") == 1  # 空文字的调用不记
    assistant_items = [item for item in agent.session["history"] if item["role"] == "assistant"]
    assert [item["content"] for item in assistant_items] == ["I'll read calc.py first.", "Fixed mean() to use true division."]


def test_long_native_preamble_is_recorded_whole_without_a_truncation_marker(tmp_path):
    text = "x" * 5000 + " END"
    agent = build(tmp_path, [native(text, "read_file", path="calc.py"), "done reading"])

    agent.ask("look at calc.py")

    assistant = [item["content"] for item in agent.session["history"] if item["role"] == "assistant"]
    assert assistant == [text, "done reading"]  # 改动撤掉时这里只有 "done reading"
    assert "truncated" not in agent.model_client.prompts[-1]


def test_rules_name_the_last_turn_as_the_current_one(tmp_path):
    agent = build(tmp_path, ["ok then"])
    agent.ask("hi")

    prompt = agent.model_client.prompts[-1]
    assert "The last Turn in the transcript is the turn you are working on now" in prompt
    assert prompt.index("The last Turn in the transcript") < prompt.index("Tools:")  # 在不变的 system 部分里


def test_empty_recall_no_longer_calls_itself_prior_conversation_evidence(tmp_path):
    assert render_prompt_recall([]) == "Recovered transcript snippets:\n- none"
    agent = build(tmp_path, [native("", "read_file", path="calc.py"), "read it"])
    agent.ask("read calc.py")
    assert "prior conversation evidence" not in agent.model_client.prompts[-1]


def test_recall_with_matches_keeps_the_explanation():
    text = render_prompt_recall([{"turn_id": "t1", "event_ids": ["evt_1"], "text": "earlier: changed limits.py"}])
    assert "prior conversation evidence" in text


@pytest.mark.parametrize(
    "answer",
    [
        "I can see from the transcript that this task has already been completed in a prior session. The test failure has been fixed",
        "我看到了情况。根据转录记录，这个任务已经完成了：",
        "看到之前的任务已经完成了。`slugify` 函数已按照 docstring 严格实现",
        "I've already completed the implementation in the prior context.",
        "I see the task is already complete! The changes have been successfully applied and tested:",
        "I can see the task has been completed successfully. The transcript shows:",
    ],
)
def test_stale_openers_from_stored_bench_answers_are_flagged(answer):
    assert stale_claim(answer)


@pytest.mark.parametrize(
    "answer",
    [
        "已完成修复。问题在于原始的 `slugify()` 函数只是将字符串分割和连接",
        "The last run of pytest passed: 4 passed in 0.02s.",
        "根据历史记录，这个函数在 v1 里返回整数，现在改成了浮点数。",
        "问题已解决。`mean()` 之前用的是整数除法 `//`，现在改成了 `/`。",
        "The previous implementation used integer division; I changed it to true division.",
        "Fixed. The function was already imported in api.py, so only store.py changed.",
        "",
    ],
)
def test_honest_answers_are_not_flagged(answer):
    assert not stale_claim(answer)


def test_only_the_opening_of_the_answer_is_checked():
    assert not stale_claim("Fixed calc.py.\n" + "details " * 80 + "this has already been completed in a prior session")


def test_bench_summary_counts_stale_rows():
    rows = [
        {"task": "a", "passed": True, "stale_claim": "from the transcript", "steps": 3, "wall_seconds": 1.0},
        {"task": "b", "passed": False, "stale_claim": "", "steps": 3, "wall_seconds": 1.0},
    ]
    summary = report.summarize(rows)
    assert summary["stale_claims"] == 1
    assert any("误认前情：1 行" in line for line in report._claim_lines(summary))


def test_calibration_fixture_stored_haiku_answers_regrade_to_eleven_of_thirty():
    # 校准用：正则就是照着这 30 行写的，这里只防回归，不当作正则准确率的证据
    names = ["20261101-103524", "20261101-105828", "20261101-130319"]
    root = Path(__file__).resolve().parents[1] / "benchmarks" / "results"
    rows = []
    for name in names:
        path = next(root.glob(f"{name}-*.json"))
        rows.extend(json.loads(path.read_text(encoding="utf-8"))["runs"])
    assert len(rows) == 30
    assert sum(1 for row in rows if stale_claim(row["answer"])) == 11
