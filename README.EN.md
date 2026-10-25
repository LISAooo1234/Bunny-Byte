<div align="center">

# Bunny Byte

[中文](README.md) | [English](README.EN.md)

**A terminal coding agent for local repositories**

Bunny Byte provides TUI / REPL / CLI interfaces with controlled tool use,
session recovery, task planning, long-term memory, and local run artifacts.  
The goal is not only to connect a model to a terminal, but to make a local
agent controllable, recoverable, and observable.

</div>

<p align="center">
  <img src="assets/screenshots/bunnybyte-tui-intro.png" alt="Bunny Byte TUI intro screen" width="960">
</p>

> BunnyByte is AI and can make mistakes. Please double-check responses.

## Overview

Bunny Byte runs inside a local repository and uses controlled tools for code
search, command execution, file editing, task decomposition, and context
management. Session data, events, traces, checkpoints, reports, and memory are
stored locally for recovery, review, and debugging.

## Core Capabilities

| Capability | Summary |
| --- | --- |
| Model integration | Supports the OpenAI Responses API and Anthropic-style interfaces; `provider` selects configuration and `protocol` selects the request format. |
| Provider failover | When the primary provider returns 5xx / rate limits / timeouts, requests switch to the configured `fallback` chain; the switch is shown in the UI and recorded in the run trace. See [Configuration](docs/configuration.md). |
| Tool calling | Uses provider-native tool calling when available and keeps a fallback path for legacy backends. |
| Session lifecycle | Supports `/history`, `/resume`, `/fork`, `/rollback`, and `/compact`. |
| Task orchestration | Includes plan mode, todo ledger, `ask_user`, and `Explore` / `worker` subagents. |
| Memory | Includes working memory, daily logs, durable topics, `/remember`, and `/dream`. |
| Benchmark | `bunny bench` scores the agent with a real model on 8 fixed tasks (pass rate, steps, tokens, wall time) and appends results to `benchmarks/results/`. See [Benchmark](docs/benchmark.md). |
| Safety controls | Supports approval policy, sandboxing, and fresh-read validation before file writes. |
| Run artifacts | Persists session JSON, events JSONL, traces, task state, reports, and checkpoints. |

## Interface

### Tool execution and task flow

<p align="center">
  <img src="assets/screenshots/bunnybyte-tui-tools.png" alt="Bunny Byte TUI tool execution" width="960">
</p>

The same interface shows model output, tool calls, command results, and
sub-task state.

### Planning and memory state

<p align="center">
  <img src="assets/screenshots/bunnybyte-tui-memory-skills.png" alt="Bunny Byte TUI planning and memory" width="960">
</p>

Plan mode can display todo progress, subagent state, and memory-related context
at the same time.

## Architecture

```mermaid
flowchart TB
    U["User"] --> UI["TUI / REPL / CLI"]
    UI --> E["Runtime Engine"]
    E --> C["Context Manager"]
    C --> P["Prompt Assembly"]
    E --> M["Model Client"]
    M --> PV["OpenAI-compatible / Anthropic-compatible Providers"]
    E --> T["Tool Registry"]
    T --> W["Workspace / Git / Files"]
    E --> S["Session Store"]
    E --> R["Run Store"]
    S --> SE["session.json / events.jsonl"]
    R --> RE["trace / task_state / report / checkpoints"]
```

See the full version in [Architecture](docs/architecture.md).

<details>
<summary>Expand architecture preview</summary>

```text
┌────────────────────────────────────────────────────────────────────────────────────┐
│                              ENTRY LAYER (入口层)                                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────────┐ │
│  │  bunny   │  │bunnybyte │  │   bb     │  │bunnybyte │  │  bunny "one-shot"    │ │
│  │  (TUI)   │  │  (TUI)   │  │  (TUI)   │  │  --repl  │  │  --resume latest     │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────────┬───────────┘ │
│       └──────────────┴─────────────┴─────────────┘                │              │
│                              │                                     │              │
│                     bunnybyte.cli:main()                            │              │
│                     argparse → runtime.init →                      │              │
│                     TUI / REPL / one-shot 分支                      │              │
└──────────────────────────────────┬─────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│                              CORE RUNTIME (核心运行时)                               │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐   │
│  │                          BunnyByte (core/runtime.py)                         │   │
│  │                                                                              │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │   │
│  │  │ SessionState │  │ WorkspaceCtx │  │  Permission  │  │  Tool Profiles   │ │   │
│  │  │   Mixin      │  │  (git snapshot│  │   Checker    │  │  (default/plan/  │ │   │
│  │  │              │  │   +docs)     │  │              │  │   readonly/worker)│ │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────────┘ │   │
│  │                                                                              │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │   │
│  │  │  CompactMgr  │  │ ContextMgr   │  │  PlanMode    │  │   WorkerManager  │ │   │
│  │  │  (history    │  │  (prompt     │  │  Controller  │  │   (subagent      │ │   │
│  │  │   compaction)│  │   assembly)  │  │              │  │    lifecycle)    │ │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────────┘ │   │
│  │                                                                              │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐ │   │
│  │  │  ReadLedger  │  │  TodoLedger  │  │  RunStore    │  │  SessionEventBus │ │   │
│  │  │  (file read  │  │  (task todo  │  │  (evidence   │  │  (event stream   │ │   │
│  │  │   dedup)     │  │   tracking)  │  │   persistence)│  │   persistence)  │ │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────────┘ │   │
│  │                                                                              │   │
│  │  ┌──────────────────────────────────────────────────────────────────────┐   │   │
│  │  │                    Engine (core/engine.py)                            │   │   │
│  │  │                                                                       │   │   │
│  │  │   run_turn() ─── 生成器，每次 yield 一个事件                           │   │   │
│  │  │                                                                       │   │   │
│  │  │   ┌──────────────────────────────────────────────────────────────┐   │   │   │
│  │  │   │                  TURN CONTROL LOOP                           │   │   │   │
│  │  │   │                                                              │   │   │   │
│  │  │   │   1. ContextManager 组装 prompt                              │   │   │   │
│  │  │   │   2. Provider Client 发起请求 (流式)                          │   │   │   │
│  │  │   │   3. 解析返回 (原生 tool call / 文本 <tool>/<final>)          │   │   │   │
│  │  │   │   4. ToolExecutor 执行工具 (含 policy + permission 检查)      │   │   │   │
│  │  │   │   5. 结果追加到 history，循环直到 <final> 或达到步数上限      │   │   │   │
│  │  │   │                                                              │   │   │   │
│  │  │   └──────────────────────────────────────────────────────────────┘   │   │   │
│  │  └──────────────────────────────────────────────────────────────────────┘   │   │
│  └─────────────────────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────────────────┐
│                              INFRASTRUCTURE (基础设施层)                             │
│                                                                                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │  Providers   │  │    Tools     │  │   Memory     │  │      Skills          │   │
│  │  (API适配)   │  │  (工具系统)   │  │  (记忆系统)   │  │     (技能系统)        │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────────────┘   │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐   │
│  │   Sandbox    │  │  Evaluation  │  │   Testing    │  │    Evidence          │   │
│  │  (安全隔离)   │  │  (评估系统)   │  │  (脚本化测试) │  │   (审计证据)          │   │
│  └──────────────┘  └──────────────┘  └──────────────┘  └──────────────────────┘   │
└────────────────────────────────────────────────────────────────────────────────────┘
```

</details>

Rough code mapping:

- [`bunnybyte/tui/`](bunnybyte/tui/): Textual terminal interface
- [`bunnybyte/core/`](bunnybyte/core/): runtime, engine, context, session, checkpoints, workers
- [`bunnybyte/tools/`](bunnybyte/tools/): tool registry, schemas, execution entrypoints
- [`bunnybyte/features/`](bunnybyte/features/): memory, skills, sandbox
- [`bunnybyte/providers/`](bunnybyte/providers/): model provider clients
- [`bunnybyte/evaluation/`](bunnybyte/evaluation/): evaluation and run evidence

## Quick Start

Requirements: `Python 3.10+`, `git`, and at least one usable model provider
key.

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/LISAooo1234/Bunny-Byte/main/install.sh | bash
```

Then initialize provider settings:

```bash
bunny setup
bunny doctor
bunny
```

`bunny doctor` checks Python, config files, provider/model/API key (only the last 4 characters are shown), live connectivity, the git repo, the sandbox backend and ripgrep, and flags `.bunnybyte.toml` / `.env` files tracked by git that could leak keys. Every failing check comes with an actionable fix; `--offline` skips the network request, `--json` is meant for scripts and CI, and the exit code is 1 when any check fails.

### Source setup

```bash
git clone https://github.com/LISAooo1234/Bunny-Byte.git
cd Bunny-Byte
uv sync --dev
uv run bunny
```

Available entrypoints:

```bash
bunny
bunnybyte
bb
bunnybyte-tui
bbtui
```

## Common Commands

### CLI

```bash
bunny
bunny --resume latest
bunny --repl
bunny "Fix the first failing test in the current repository"
bunny doctor            # environment self-check
bunny bench             # live benchmark (calls the real model)
```

### TUI Slash Commands

- `/help`
- `/history`
- `/resume latest`
- `/fork <event|turn|#>`
- `/rollback <event|turn|#>`
- `/plan <topic>`
- `/plan-exit`
- `/skills`
- `/tool [name]`
- `/memory`
- `/working-memory`
- `/dream`

## Benchmark

Bunny Byte ships a live benchmark. `bunny bench` has a real model solve 8 tasks in throwaway git repositories (fix a failing test, implement a function from its docstring, rename across files, add a CLI flag, answer a read-only codebase question, recover from a rejected patch, ...). A shell verifier, including hidden tests that only exist at grading time, decides each result. The command reports pass rate, clean-finish rate, average steps, tokens and wall time, and appends a result file tagged with version, git commit, model and task-set fingerprint to `benchmarks/results/`, so the score history is public.

<<BASELINE>>

There are only 8 tasks, so one task is 12.5 percentage points and a single run can move by one task as noise; average steps and tokens are the more sensitive regression signals. Details: [Benchmark](docs/benchmark.md) (Chinese).

## Repository Layout

```text
bunnybyte/
├── core/        # runtime, engine, context, session, checkpoints, workers
├── tools/       # built-in tools and registration
├── tui/         # Textual terminal interface
├── features/    # memory, skills, sandbox
├── providers/   # model provider clients
├── bench/       # bunny bench live benchmark
└── evaluation/  # evaluation and run artifacts

docs/
├── configuration.md
├── skills.md
├── memory.md
├── sandbox.md
└── benchmark.md

tests/           # runtime / safety / TUI / acceptance / regression tests
```

## Documentation

- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Skills](docs/skills.md)
- [Memory](docs/memory.md)
- [Sandbox](docs/sandbox.md)
- [Benchmark](docs/benchmark.md) (Chinese)
