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

## 环境变量

不写 toml 也能跑——只设环境变量即可：

| 变量 | 用途 |
|------|------|
| `BUNNYBYTE_PROVIDER` | 默认 provider |
| `BUNNYBYTE_API_KEY` / `BUNNYBYTE_BASE_URL` / `BUNNYBYTE_MODEL` | 通用 override |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | Anthropic |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI |
| `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` | DeepSeek |

兼容历史 `.env`：`BUNNYBYTE_OPENAI_*` / `BUNNYBYTE_ANTHROPIC_*` / `BUNNYBYTE_DEEPSEEK_*` 仍然能用。

## CLI 参数

```bash
bunny --provider deepseek --model deepseek-v4-pro
bunny --api-key sk-... --base-url https://...
bunny --max-steps 50
bunny --max-new-tokens 4096  # 可选：显式限制每步最大输出 token
bunny --temperature 0.0
bunny --approval ask          # ask | auto | never
bunny --sandbox best_effort   # off | best_effort | required
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
