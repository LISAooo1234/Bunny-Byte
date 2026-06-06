import importlib.util
import sys
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compact_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("compact_benchmark", _SCRIPT)
compact_benchmark = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = compact_benchmark
_SPEC.loader.exec_module(compact_benchmark)


def test_compact_benchmark_reports_compression_and_preserves_history():
    row = compact_benchmark.measure_compaction(turns=12, keep_recent_turns=3)

    assert row.turns == 12
    assert row.history_items > 12
    assert row.before_prompt_chars > row.after_prompt_chars
    assert row.before_tokens > row.after_tokens
    assert row.prompt_compression_ratio > 0
    assert row.token_compression_ratio > 0
    assert row.summary_chars > 0
    assert row.history_preserved is True


def test_compact_benchmark_formats_rows():
    row = compact_benchmark.measure_compaction(turns=8, keep_recent_turns=2)
    output = compact_benchmark.format_rows([row])

    assert "turns | items | keep" in output
    assert "tok saved" in output
    assert "yes" in output
