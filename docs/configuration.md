# 配置

bunnybyte 默认使用全局配置。普通用户只需要运行一次 `bunny setup`，以后在任意项目目录都可以直接 `bunny`。

配置按下面这个优先级合并：

```
CLI 显式参数 > 环境变量 > 项目 .bunnybyte.toml > 全局 ~/.config/bunnybyte/config.toml > 代码默认
```

## Provider profile

provider 是 TOML 里的一段配置 profile，名字（如 `deepseek` `openai` `anthropic`）只用于人类辨识；真正决定走哪个协议的是 `protocol` 字段，目前支持 `openai` 和 `anthropic` 两种。

## 新手路径：全局配置

交互式配置：

```bash
bunny setup
```

非交互配置：

```bash
bunny setup --provider deepseek --api-key sk-...
```

这会写入：

```text
~/.config/bunnybyte/config.toml
```

查看配置：

```bash
bunny config show
bunny config path
```

生成的全局配置示例：

```toml
provider = "deepseek"

[providers.deepseek]
protocol = "anthropic"
api_key = "sk-..."
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-pro"
```

切 provider：

```bash
bunny                       # 用全局配置里的默认 provider
bunny --provider openai     # 临时切换
bunny --provider anthropic --model claude-opus-4-6
```

## 项目级覆盖（可选）

只有某个仓库必须使用不同 provider 或不同模型时，才需要项目 `.bunnybyte.toml`。它会覆盖全局配置。

放在仓库根目录，**不要提交真实 key**（默认已被 `.gitignore` 忽略）：

```toml
provider = "openai"

[providers.openai]
protocol = "openai"
api_key = "sk-..."
base_url = "https://api.openai.com/v1"
model = "gpt-5.4"

[providers.anthropic]
protocol = "anthropic"
api_key = "sk-ant-..."
base_url = "https://api.anthropic.com"
model = "claude-sonnet-4-6"
```

## 备用 provider（故障切换）

主 provider 返回 5xx、限流、超时或网络错误，并且重试仍然失败时，Bunny Byte 会自动改用备用 provider 完成这一次请求，并在界面提示、run trace（`provider_fallback` 事件）和会话事件里留下记录。切换后主 provider 会冷却 120 秒，期间直接使用备用，工具循环不会每一步都重新等一轮重试；冷却结束后自动回到主 provider。

```toml
provider = "openai"
fallback = ["deepseek"]          # 顶层默认备用链

[providers.openai]
fallback = ["deepseek", "anthropic"]   # 可选：只对这个 profile 生效，优先于顶层
```

也可以用环境变量 `BUNNYBYTE_FALLBACK=deepseek,anthropic` 或命令行 `--fallback deepseek`；`none` 表示关闭。优先级：CLI > 环境变量 > profile 内的 `fallback` > 顶层 `fallback`。

注意：

- 只有可重试的服务端/网络错误才会切换；鉴权失败（401/403）、请求格式错误不会悄悄换模型，避免掩盖配置问题。
- 回答已经开始流式输出之后发生的错误不会切换，以免内容重复。
- 备用 provider 必须配置完整（api_key、base_url、model），不完整的会被忽略，`bunny doctor` 会提示。
- `BUNNYBYTE_API_KEY` / `BUNNYBYTE_BASE_URL` / `BUNNYBYTE_MODEL` 这类通用 override 只作用于主 provider，不会套到备用上。
- 主备可以跨协议（例如 openai 主、anthropic 协议的 deepseek 备），工具定义会自动转换格式。

## 验证回执（`[verify]`）

模型改了代码、准备给出最终回答时，运行时会先跑项目自己的检查并留下回执（说明见 README「验证回执」）。

```toml
[verify]
mode = "ask"                  # off | ask | auto，默认 ask：遵循审批策略
commands = ["pytest -q"]      # 可选；不写则按本回合改动的文件自动探测
timeout = 120                 # 单条命令秒数，1-1800
max_repair_rounds = 1         # 验证失败后让模型修复的轮数，0-5；0 表示只记录不重试
fix_proof = "report"          # off | report | repair：验证通过后撤回源码、用旧代码重跑改过的测试
```

- `ask`：`--approval auto` 直接跑，`--approval ask` 会先询问，`--approval never` 不跑；`auto` 一律直接跑；`off` 关闭。
- 只对主 agent 生效；子 agent、plan mode、只读模式、纯文档改动不会触发。
- 配置了 `[sandbox]` 时验证命令走同一个沙箱；`CI=1` 会注入环境。
- 回执写入 `task_state.json`、`report.json` 的 `verification` 字段和 trace 的 `verification_run` 事件。
- `fix_proof`：验证通过、本轮同时改了源码和测试时，在临时副本里只撤回源码、重跑改过的测试（说明见 README「修复证明」）。`report` 只记录（`verification.fix_proof`、trace `fix_proof` 事件），`repair` 在“撤回后测试照样通过”时退回给模型一轮，`off` 关闭。开了沙箱时跳过。

## 独立审查（`[critic]`）

模型改了代码、准备交卷时，另起一次不带工具的模型调用，对照用户请求、项目规则（AGENTS.md 等）和本轮 diff 逐条核对，有证据的意见退回给模型修复（说明见 README「独立审查」）。默认关闭。

```toml
[critic]
mode = "auto"                 # off（默认）| auto，也可用 --critic 覆盖
profile = "oneapi"            # 可选：审查用的 provider 配置名；不写就用当前主模型
max_rounds = 1                # 有反对意见时退回修复的轮数，1-3
max_diff_chars = 12000        # 发给审查模型的 diff 上限（超出部分会告诉它看不到）
```

- 在验证闸门之后、`on_final` 钩子之前运行；只对主 agent 生效，子 agent、plan mode、只读模式、本回合没改文件时不触发；剩余步数不足 3 步时跳过（修复需要工具步数）。
- 修复轮不占模型调用预算；模型没再改文件、只在回答里反驳时不会再审一遍。审查调用出错、回复不是 JSON、反对意见没写文件和证据，一律按通过处理并记下原因。
- 发出去的 diff 先按 secret 环境变量脱敏；`profile` 指向别的 provider 时，diff 会发给那个 provider。
- 结果写入 `task_state.json` / `report.json` 的 `critic` 字段和 trace 的 `critic_review` 事件（含 token，`bunny bench` 与 `bunny -p` 的用量统计会算进去）。

## 钩子（`[[hooks.*]]`）

`[[hooks.pre_tool]]`、`[[hooks.post_tool]]`、`[[hooks.on_final]]` 三类 shell 钩子，字段 `command`（必填）、`match`（工具名，`|` 分隔，支持 `*`）、`timeout`（1–300 秒，默认 10）、`on_error`（`allow` 默认 / `block`）、`only_if_changed`（仅 on_final）；`[hooks] max_final_rounds`（0–5，默认 2）。仓库里的 `.bunnybyte.toml` 的钩子要 `/hooks trust` 之后才运行，全局配置和 `--config` 直接生效。完整说明见 [hooks.md](hooks.md)。

## 项目规则

没有配置项，放文件即可：仓库根目录（以及启动目录）下的 `AGENTS.md`、`CLAUDE.md`、`.bunnybyte/rules/*.md` 会进入提示词前缀。每个文件最多 6000 字、合计最多 12000 字，超出部分会提示去 `read_file`；`bunny doctor` 的“项目规则”一行显示实际读到的文件。规则不会放宽工具权限。详见 README「项目规则」。

## 环境变量

不写 toml 也能跑——只设环境变量即可：

| 变量 | 用途 |
|------|------|
| `BUNNYBYTE_PROVIDER` | 默认 provider |
| `BUNNYBYTE_API_KEY` / `BUNNYBYTE_BASE_URL` / `BUNNYBYTE_MODEL` | 通用 override（只作用于主 provider） |
| `BUNNYBYTE_FALLBACK` | 备用 provider 链，逗号分隔 |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | Anthropic |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | DeepSeek |

兼容历史 `.env`：`BUNNYBYTE_OPENAI_*` / `BUNNYBYTE_ANTHROPIC_*` / `BUNNYBYTE_DEEPSEEK_*` 仍然能用。

## CLI 参数

```bash
bunny --provider deepseek --model deepseek-v4-pro
bunny --api-key sk-... --base-url https://...
bunny --fallback deepseek     # 主 provider 故障时改用 deepseek；none 关闭
bunny --max-steps 50
bunny --max-new-tokens 4096  # 可选：显式限制每步最大输出 token
bunny --temperature 0.0
bunny --approval ask          # ask | auto | never
bunny --sandbox best_effort   # off | best_effort | required
bunny --verify auto           # off | ask | auto：交卷前自动跑项目测试
bunny --no-auto-dream         # 关闭后台 memory 整合
bunny --cwd /path/to/repo     # 切换工作目录
bunny --resume latest         # 续接上一个 session
bunny --config /path/to/custom.toml
```

跑 `bunny --help` 看完整参数。

## 默认值速查

| 项 | 默认 |
|----|------|
| `max-steps` | 50 |
| `max-new-tokens` | 默认不设置上限；只有显式传入 `--max-new-tokens` 时才下发限制 |
| `temperature` | 0.2 |
| `approval` | `ask` |
| `sandbox` | `off` |
| `dream-interval` | 24 小时 |
| `dream-min-sessions` | 5 |

## 输出 token 上限

默认情况下，Bunny Byte 不主动向 provider 下发 `max_tokens` / `max_output_tokens`，让模型按 provider 自身策略输出。只有用户显式传入时才限制：

```bash
bunny --max-new-tokens 32000
```

如果某个兼容后端要求必须传输出上限，或者你希望控制成本和响应长度，可以显式设置该参数。

## 调试

- `/session` 查看 session 文件路径和当前 runtime 标识
- `/context` 查看上下文用量切片
- `/usage` 查看 token / call 数
- 所有事件流写到 `.bunnybyte/sessions/<id>.events.jsonl`，可以用 `tail -f` 观察
- 每次运行的 trace 在 `.bunnybyte/runs/<run_id>/trace.jsonl`
