"""bunny replay：在终端里重看一次运行（.bunnybyte/runs/<run_id>）的时间线，--diff 附上那一轮的改动。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .core.secret_vault import SecretVault
from .core.session_topics import _is_synthetic_worker_prompt
from .core.shadow_snapshots import ShadowError, ShadowStore
from .core.turn_diff import render_changes, tree_changes
from .replay_render import STATUS_TEXT, render_timeline
from .tui.tool_cards import clip_cells

# 旧版本的 report 没有 session_kind：按 dream / 技能演化 / 子 agent 固定的请求开头认出内部运行
INTERNAL_REQUEST_PREFIXES = (
    "# Dream: Memory Consolidation",
    "# Dream Retry",
    "You are BunnyByte's skill self-evolution",
    "You are BunnyByte's independent skill-review",
)
INDEX_DIGITS = 4  # `bunny replay 3` 是序号，`bunny replay 20261031` 是 run id 开头
DEFAULT_LIST = 15


def build_replay_parser():
    parser = argparse.ArgumentParser(
        prog="bunny replay",
        description="重看一次运行的时间线：模型调用、工具、验证、审查、钩子、改动的文件和最终回答。",
    )
    parser.add_argument("run", nargs="?", default="latest", help="run id（可只写开头）、序号（1 = 最近一次）或 latest。")
    parser.add_argument("--list", action="store_true", help="列出最近的运行。")
    parser.add_argument("--diff", action="store_true", help="附上这一轮改动文件的 diff（需要影子快照）。")
    parser.add_argument("--all", action="store_true", help="也包括子 agent、记忆整理、技能演化这些内部运行。")
    parser.add_argument("-n", "--limit", type=int, default=DEFAULT_LIST, help="--list 显示的条数。")
    parser.add_argument("--cwd", default=".", help="工作区目录（在它的 .bunnybyte/runs 里找）。")
    return parser


def run_replay_command(argv=None, stdout=None):
    stdout = stdout or sys.stdout
    args = build_replay_parser().parse_args(argv)
    root = workspace_root(args.cwd)
    runs_dir = root / ".bunnybyte" / "runs"
    runs = list_runs(runs_dir, include_internal=args.all)
    if args.list:
        print(render_run_list(runs[: max(1, args.limit)], runs_dir, args.all), file=stdout)
        return 0
    run, error = resolve_run(runs, args.run, runs_dir)
    if run is None:
        print(error, file=sys.stderr)
        return 2
    events = read_trace(run["path"])
    text = render_timeline(events, run["report"], {"run_id": run["id"], **(run["state"] or {})})
    if args.diff:
        text += "\n\n" + render_run_diff(root, run["report"], run["state"])
    print(text, file=stdout)
    return 0


def workspace_root(cwd):
    """从 cwd 往上找带 .bunnybyte/runs 的目录（在子目录里运行也能找到），找不到就用 cwd。

    到 git 仓库根或家目录为止：不能跑到 `~/.bunnybyte/runs`，把别的项目的运行当成这个项目的。
    """
    start = Path(cwd).expanduser().resolve()
    home = Path.home().resolve()
    for candidate in (start, *start.parents):
        if candidate == home and candidate != start:
            break
        if (candidate / ".bunnybyte" / "runs").is_dir():
            return candidate
        if (candidate / ".git").exists():
            break
    return start


def _load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_trace(run_dir):
    events = []
    try:
        lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue  # 进程被杀时最后一行可能只写了一半
        if isinstance(event, dict):
            events.append(event)
    return events


def is_internal(report, state):
    kind = report.get("session_kind")
    if kind:
        return kind != "main" or int(report.get("depth") or 0) > 0
    request = str(state.get("user_request") or "")
    return request.startswith(INTERNAL_REQUEST_PREFIXES) or _is_synthetic_worker_prompt(request)


def _started_at(path):
    """run id 只精确到秒（后缀是随机的）：同一秒里的几次运行按 trace 第一行的微秒时间排。"""
    try:
        with (path / "trace.jsonl").open(encoding="utf-8") as handle:
            return str(json.loads(handle.readline()).get("created_at", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def list_runs(runs_dir, include_internal=False):
    """最新在前。"""
    if not runs_dir.is_dir():
        return []
    runs = []
    candidates = [p for p in runs_dir.iterdir() if p.is_dir() and p.name.startswith("run_")]
    for path in sorted(candidates, key=lambda item: (item.name[4:19], _started_at(item)), reverse=True):
        report = _load_json(path / "report.json")
        state = report.get("task_state") or _load_json(path / "task_state.json")
        if not state and not (path / "trace.jsonl").exists():
            continue
        internal = is_internal(report, state)
        if internal and not include_internal:
            continue
        runs.append({"id": path.name, "path": path, "report": report, "state": state, "internal": internal})
    return runs


def resolve_run(runs, target, runs_dir):
    target = str(target or "latest").strip()
    if not runs:
        return None, f"{runs_dir} 里还没有运行记录。"
    if target == "latest":
        return runs[0], ""
    if target.isdigit() and len(target) <= INDEX_DIGITS:
        index = int(target)
        if 1 <= index <= len(runs):
            return runs[index - 1], ""
        return None, f"只有 {len(runs)} 次运行，`bunny replay --list` 查看。"
    direct = runs_dir / target
    if direct.is_dir() and target.startswith("run_"):
        state = _load_json(direct / "task_state.json")
        report = _load_json(direct / "report.json")
        return {"id": target, "path": direct, "report": report, "state": report.get("task_state") or state}, ""
    matches = [run for run in runs if run["id"].startswith(target) or run["id"].startswith("run_" + target)]
    if len(matches) == 1:
        return matches[0], ""
    if matches:
        return None, f"「{target}」匹配到 {len(matches)} 次运行，请写得更长一些。"
    return None, f"找不到运行「{target}」（内部运行需要加 --all），`bunny replay --list` 查看。"


def render_run_list(runs, runs_dir, include_internal):
    if not runs:
        return f"{runs_dir} 里还没有运行记录。"
    lines = ["#   开始时间             状态      步数  请求"]
    for number, run in enumerate(runs, start=1):
        state = run["state"]
        status = STATUS_TEXT.get(str(state.get("status", "")), str(state.get("status", "") or "?"))
        status = "未结束" if state.get("status") == "running" else status
        request = " ".join(str(state.get("user_request", "")).split())
        mark = "[内部] " if run["internal"] else ""
        steps = state.get("tool_steps", "")
        lines.append(f"{number:<3} {_run_time(run['id']):<20} {clip_cells(status, 8):<8} {steps!s:>4}  {mark}{clip_cells(request, 60)}")
    hint = "" if include_internal else "（不含子 agent / 记忆整理等内部运行，加 --all 显示）"
    lines.append(f"\n`bunny replay <序号或 run id>` 查看时间线，加 --diff 看改动。{hint}")
    return "\n".join(lines)


def _run_time(run_id):
    try:
        return datetime.strptime(run_id[4:19], "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return run_id


def render_run_diff(root, report, state=None):
    changes = report.get("changes") or {}
    if not changes.get("before_tree") or not changes.get("after_tree"):
        paths = (state or report.get("task_state") or {}).get("changed_paths") or []
        if not paths:
            return "（这次运行没有改动文件。）"
        return "（这次运行没有记录快照 tree，可能来自 0.10.0 之前的版本，或快照不可用；只知道改过：" + "、".join(paths[:12]) + "）"
    store = ShadowStore.for_root(root)
    if not store.available:
        return f"（无法显示 diff：git 快照不可用，{store.disabled_reason}）"
    try:
        items = tree_changes(store, changes["before_tree"], changes["after_tree"])
    except (ShadowError, OSError, ValueError) as exc:
        return f"（无法显示 diff：快照读不出来，可能 .bunnybyte/shadow.git 被清理过。{clip_cells(str(exc), 80)}）"
    if not items:
        return "（这次运行没有改动文件。）"
    vault, known = SecretVault(), known_secrets(root)
    body = render_changes(items, mask=lambda text: vault.mask(text, known))
    return "改动 diff（密钥已按格式遮蔽）：\n\n" + body


def known_secrets(root):
    """遮蔽 diff 时已知的密钥值：名字像密钥的环境变量，加上各 provider 配置里的 api_key。"""
    from .core.runtime_secrets import RuntimeSecretsMixin

    items = [
        (name, value)
        for name, value in os.environ.items()
        if value and RuntimeSecretsMixin.looks_sensitive_env_name(name)
    ]
    try:
        from .config import list_provider_profiles, resolve_provider_config

        for profile in list_provider_profiles(start=root):
            key = resolve_provider_config(profile.name, start=root).api_key
            if key and key.strip():
                items.append(("api_key", key.strip()))
    except Exception:
        pass  # 配置坏了也要能看 diff；只剩按格式识别
    return items
