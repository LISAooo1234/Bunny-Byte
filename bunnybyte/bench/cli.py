"""`bunny bench` 子命令：解析参数、构造真实模型 client、跑任务集、落盘并打印结果。"""

import argparse
import json
import sys
from pathlib import Path

from .report import (
    pad,
    build_record,
    load_history,
    previous_comparable,
    render_history,
    render_record,
    render_row,
    save_record,
)
from .runner import DEFAULT_MAX_STEPS, DEFAULT_TASK_TIMEOUT, run_bench
from .tasks import select_tasks, task_set_hash

DEFAULT_BENCH_PROVIDER = "deepseek"
BENCH_TEMPERATURE = 0.0
BENCH_REQUEST_TIMEOUT = 120


def build_bench_parser():
    parser = argparse.ArgumentParser(
        prog="bunny bench",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description=(
            "用真实模型在临时仓库里跑一组固定编码任务，用 shell verifier 判分，"
            "输出通过率、步数、token 与耗时，并把结果追加到 benchmarks/results/。"
            "任务以自动批准模式运行在临时目录中，会真实调用模型并产生费用。"
        ),
    )
    parser.add_argument("--provider", default=DEFAULT_BENCH_PROVIDER, help="使用的 provider 配置（bench 不启用 fallback，避免污染分数）。")
    parser.add_argument("--model", default=None, help="覆盖 provider 配置里的模型名。")
    parser.add_argument("--config", default=None, help="BunnyByte TOML 配置文件路径。")
    parser.add_argument("--cwd", default=".", help="从该目录向上查找 .bunnybyte.toml。")
    parser.add_argument("--tasks", default=None, help="只运行指定任务，逗号分隔。")
    parser.add_argument("--repeat", type=int, default=1, help="每个任务重复次数；次数越多分数越稳。")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="agent 每题的工具步数硬上限（默认与正常 CLI 一致）。")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TASK_TIMEOUT, help="单个任务的墙钟上限（秒）。")
    parser.add_argument("--verify", action="store_true", help="给 agent 开启验证闸门（交卷前自动跑探测到的测试，失败可修复 1 轮）；结果单独对比。")
    parser.add_argument("--out", default=None, help="结果目录；默认 ./benchmarks/results（存在时），否则 ~/.bunnybyte/bench-results。")
    parser.add_argument("--no-save", action="store_true", help="只打印，不写结果文件。")
    parser.add_argument("--save", action="store_true", help="用 --tasks 只跑部分任务时默认不写结果文件；加上它才保存。")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整结果。")
    parser.add_argument("--list", action="store_true", help="列出任务后退出。")
    parser.add_argument("--history", action="store_true", help="显示历史分数后退出。")
    parser.add_argument("--fail-under", type=float, default=None, help="通过率（0-1）低于该值时返回退出码 1。")
    return parser


def resolve_out_dir(value):
    if value:
        return Path(value).expanduser()
    local = Path.cwd() / "benchmarks" / "results"
    if local.is_dir() and (Path.cwd() / "bunnybyte" / "bench" / "tasks.py").is_file():
        return local  # 只有在 Bunny Byte 源码目录里才写进仓库，别往别人的项目里塞文件
    return Path.home() / ".bunnybyte" / "bench-results"


def live_client(args):
    """沿用正常 CLI 的 provider 解析，但不挂 fallback；整轮 bench 共用同一个 client。"""
    from ..cli import _model_client_from_config, _resolve_cli_provider_config

    cli_args = argparse.Namespace(
        provider=args.provider,
        cwd=args.cwd,
        config=args.config,
        model=args.model,
        base_url=None,
        api_key=None,
        temperature=BENCH_TEMPERATURE,
        openai_timeout=BENCH_REQUEST_TIMEOUT,
    )
    config = _resolve_cli_provider_config(cli_args)
    return _model_client_from_config(cli_args, config), config.name, config.model, config.protocol


def list_text(tasks):
    lines = [f"{pad('任务 ID', 26)}{pad('类别', 10)}{pad('步数上限', 10)}说明"]
    for task in tasks:
        lines.append(f"{pad(task.id, 26)}{pad(task.category, 10)}{pad(task.step_budget, 10)}{task.title}")
    return "\n".join(lines)


def run_bench_command(argv=None, *, client_factory=None, stream=None):
    stream = stream or sys.stdout
    args = build_bench_parser().parse_args(argv)
    try:
        ids = [item.strip() for item in args.tasks.split(",") if item.strip()] if args.tasks else None
        tasks = select_tasks(ids)
        if args.repeat < 1:
            raise ValueError("--repeat 必须 >= 1")
        if args.list:
            print(list_text(tasks), file=stream)
            return 0
        out_dir = resolve_out_dir(args.out)
        if args.history:
            print(render_history(load_history(out_dir)), file=stream)
            return 0
        if client_factory is None:
            client, provider, model, protocol = live_client(args)
            client_factory = lambda _task: client  # noqa: E731
        else:
            provider, model, protocol = "scripted", "oracle", ""
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    progress = sys.stderr if args.json else stream
    print(
        f"bunny bench：{provider}/{model}，{len(tasks)} 个任务 × {args.repeat} 次，步数上限 {args.max_steps}，单题 {args.timeout}s{'，验证闸门开启' if args.verify else ''}",
        file=progress,
    )
    rows = run_bench(
        tasks,
        client_factory,
        repeat=args.repeat,
        timeout=args.timeout,
        max_steps=args.max_steps,
        on_result=lambda row: print(render_row(row), file=progress, flush=True),
        verify_gate=args.verify,
    )
    record = build_record(
        rows,
        tasks,
        provider=provider,
        model=model,
        set_hash=task_set_hash(tasks),
        repeat=args.repeat,
        max_steps=args.max_steps,
        protocol=protocol,
        verify=args.verify,
    )
    record["temperature"] = BENCH_TEMPERATURE
    previous = previous_comparable(record, load_history(out_dir))
    if args.json:
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True), file=stream)
    else:
        print("", file=stream)
        print(render_record(record, previous), file=stream)
    if not args.no_save and (args.save or not ids):
        path = save_record(record, out_dir)
        print(f"结果已写入 {path}", file=progress)
    elif ids and not args.no_save:
        print("只跑了部分任务，未写入结果文件（加 --save 才保存）。", file=progress)
    if args.fail_under is not None and record["summary"]["pass_rate"] < args.fail_under:
        return 1
    return 0
