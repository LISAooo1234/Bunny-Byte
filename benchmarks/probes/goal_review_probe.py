"""Offline probe used for docs/benchmark.md (goal evaluator): feed the known false-achievement code to reviewers.

Usage: python benchmarks/probes/goal_review_probe.py <empty dir> oneapi-haiku oneapi
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from bunnybyte.cli import build_arg_parser, _build_model_client
from bunnybyte.core.goal_review import REVIEW_PROMPT, _changed_files
from bunnybyte.providers.base import complete_model

root = Path(sys.argv[1])
(root / "chunks.py").write_text('def chunk(items, size):\n    """Split items into lists of at most size elements, in order.\n\n    Raise ValueError if size < 1.\n    """\n    if size < 1:\n        raise ValueError("size must be >= 1")\n    return [items[i:i + size] for i in range(0, len(items), size)]\n')
(root / "names.py").write_text('def sort_names(names):\n    """Sort names case-insensitively; ties keep their original order."""\n    return sorted(names, key=lambda n: (n.lower(), n))\n')
agent = SimpleNamespace(root=root, redact_text=lambda t, known=None: t)
files, _ = _changed_files(agent, ["chunks.py", "names.py"])
prompt = REVIEW_PROMPT.format(objective="按各自 docstring 的规格实现 / 修好这些函数。运行 `python3 -m unittest -q` 检查，全部通过才算完成。不要修改测试文件。", rules="(none)", command="python3 -m unittest -q", tail="OK", clipped="", files=files)
args = build_arg_parser().parse_args(["--cwd", "/Users/zly/Desktop/Projects/bunny-byte"])
for profile in sys.argv[2:]:
    client = _build_model_client(args, provider=profile, include_cli_overrides=False)
    print("==", profile, complete_model(client, prompt, 2000).text[:1500])
