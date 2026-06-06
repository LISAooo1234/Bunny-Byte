"""Benchmark BunnyByte history compaction compression ratios.

Run from the repository root:

    python scripts/compact_benchmark.py

The benchmark uses a deterministic fake model client, so it does not require API
credentials and produces stable output. It measures the model-facing prompt before
and after non-destructive compaction; the persisted session history remains full.
"""

from __future__ import annotations

import argparse
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bunnybyte import BunnyByte, SessionStore, WorkspaceContext
from bunnybyte.core.context_usage import estimate_tokens
from bunnybyte.testing import ScriptedModelClient


@dataclass(frozen=True)
class CompactBenchmarkRow:
    turns: int
    keep_recent_turns: int
    history_items: int
    preserved_event_ids: int
    summary_chars: int
    before_prompt_chars: int
    after_prompt_chars: int
    before_tokens: int
    after_tokens: int
    prompt_compression_ratio: float
    token_compression_ratio: float
    history_preserved: bool

    def as_dict(self):
        return {
            "turns": self.turns,
            "keep_recent_turns": self.keep_recent_turns,
            "history_items": self.history_items,
            "preserved_event_ids": self.preserved_event_ids,
            "summary_chars": self.summary_chars,
            "before_prompt_chars": self.before_prompt_chars,
            "after_prompt_chars": self.after_prompt_chars,
            "before_tokens": self.before_tokens,
            "after_tokens": self.after_tokens,
            "prompt_compression_ratio": self.prompt_compression_ratio,
            "token_compression_ratio": self.token_compression_ratio,
            "history_preserved": self.history_preserved,
        }


class DeterministicSummaryClient(ScriptedModelClient):
    provider = "benchmark"
    protocol = "scripted"
    model = "deterministic-summary"

    def __init__(self):
        super().__init__([])

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        return "<summary>" + deterministic_summary(prompt) + "</summary>"


def deterministic_summary(prompt: str) -> str:
    turn_count = prompt.count("Turn ")
    read_count = prompt.count("[tool:read_file]")
    shell_count = prompt.count("[tool:run_shell]")
    return "\n".join(
        [
            "1. Primary Request and Intent:",
            f"   Synthetic benchmark summary for {turn_count} compacted turns.",
            "2. Key Technical Concepts:",
            "   Context projection, non-destructive history retention, tool-result compaction, and resume-safe context_view.",
            "3. Files and Code Sections:",
            f"   Observed {read_count} file reads and {shell_count} shell commands in the summarized segment.",
            "4. Errors and fixes:",
            "   No real errors; benchmark data simulates long tool and assistant outputs.",
            "5. Problem Solving:",
            "   Earlier turns are represented by this summary while recent turns remain verbatim.",
            "6. All user messages:",
            "   User messages are summarized by benchmark turn count rather than repeated verbatim.",
            "7. Pending Tasks:",
            "   Continue from preserved recent turns.",
            "8. Current Work:",
            "   Measuring prompt-size reduction after compact.",
            "9. Optional Next Step:",
            "   Compare compression ratios across turn counts and keep_recent_turns settings.",
        ]
    )


def build_benchmark_agent(root: Path) -> BunnyByte:
    root.mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("benchmark workspace\n", encoding="utf-8")
    workspace = WorkspaceContext.build(root)
    return BunnyByte(
        model_client=DeterministicSummaryClient(),
        workspace=workspace,
        session_store=SessionStore(root / ".bunnybyte" / "sessions"),
        approval_policy="auto",
    )


def populate_synthetic_history(agent: BunnyByte, turns: int) -> None:
    for index in range(turns):
        agent.record(
            {
                "role": "user",
                "content": (
                    f"Synthetic request {index}: investigate context behavior, preserve exact user intent, "
                    f"and update compact benchmark data. " + ("user-detail " * 24)
                ),
                "created_at": f"2026-06-07T10:{index % 60:02d}:00+00:00",
            }
        )
        agent.record(
            {
                "role": "tool",
                "name": "read_file",
                "args": {"path": f"bunnybyte/core/example_{index % 5}.py"},
                "content": "file-content " * 180,
                "created_at": f"2026-06-07T10:{index % 60:02d}:10+00:00",
            }
        )
        if index % 3 == 0:
            agent.record(
                {
                    "role": "tool",
                    "name": "run_shell",
                    "args": {"command": "pytest tests/test_context_governance_acceptance.py -q"},
                    "content": "test output line\n" * 120,
                    "created_at": f"2026-06-07T10:{index % 60:02d}:20+00:00",
                }
            )
        agent.record(
            {
                "role": "assistant",
                "content": (
                    f"Synthetic answer {index}: analyzed files, recorded decisions, and identified next action. "
                    + ("assistant-detail " * 36)
                ),
                "created_at": f"2026-06-07T10:{index % 60:02d}:30+00:00",
            }
        )


def measure_compaction(turns: int, keep_recent_turns: int = 3) -> CompactBenchmarkRow:
    with tempfile.TemporaryDirectory(prefix="bunnybyte-compact-bench-") as tmp:
        agent = build_benchmark_agent(Path(tmp))
        populate_synthetic_history(agent, turns)
        original_history = list(agent.session["history"])
        before_prompt = agent.prompt("continue benchmark")
        summary = agent.compact_history(trigger="benchmark", keep_recent_turns=keep_recent_turns)
        after_prompt = agent.prompt("continue benchmark")
        before_tokens = estimate_tokens(len(before_prompt))
        after_tokens = estimate_tokens(len(after_prompt))
        return CompactBenchmarkRow(
            turns=turns,
            keep_recent_turns=keep_recent_turns,
            history_items=len(original_history),
            preserved_event_ids=len(summary.get("preserved_event_ids", [])),
            summary_chars=int(summary.get("summary_chars", 0)),
            before_prompt_chars=len(before_prompt),
            after_prompt_chars=len(after_prompt),
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            prompt_compression_ratio=_ratio(len(before_prompt), len(after_prompt)),
            token_compression_ratio=_ratio(before_tokens, after_tokens),
            history_preserved=agent.session["history"] == original_history,
        )


def run_benchmark(turns=(10, 25, 50), keep_recent_turns: int = 3):
    return [measure_compaction(turn_count, keep_recent_turns=keep_recent_turns) for turn_count in turns]


def _ratio(before: int, after: int) -> float:
    if before <= 0:
        return 0.0
    return max(0.0, 1.0 - (after / before))


def format_rows(rows: list[CompactBenchmarkRow]) -> str:
    headers = [
        "turns",
        "items",
        "keep",
        "before chars",
        "after chars",
        "chars saved",
        "before tok",
        "after tok",
        "tok saved",
        "summary chars",
        "history ok",
    ]
    lines = [" | ".join(headers), " | ".join(["---"] * len(headers))]
    for row in rows:
        lines.append(
            " | ".join(
                [
                    str(row.turns),
                    str(row.history_items),
                    str(row.keep_recent_turns),
                    str(row.before_prompt_chars),
                    str(row.after_prompt_chars),
                    f"{row.prompt_compression_ratio:.1%}",
                    str(row.before_tokens),
                    str(row.after_tokens),
                    f"{row.token_compression_ratio:.1%}",
                    str(row.summary_chars),
                    "yes" if row.history_preserved else "no",
                ]
            )
        )
    return "\n".join(lines)


def parse_args():
    parser = argparse.ArgumentParser(description="Benchmark BunnyByte compact compression ratios.")
    parser.add_argument("--turns", default="10,25,50", help="Comma-separated turn counts to benchmark.")
    parser.add_argument("--keep-recent-turns", type=int, default=3, help="Recent turns preserved verbatim after compact.")
    return parser.parse_args()


def main():
    args = parse_args()
    turns = tuple(int(value.strip()) for value in args.turns.split(",") if value.strip())
    rows = run_benchmark(turns=turns, keep_recent_turns=args.keep_recent_turns)
    print(format_rows(rows))


if __name__ == "__main__":
    main()
