<div align="center">

# Bunny Byte

[中文](README.md) | [English](README.EN.md)

**本地优先、可审计、有记忆的终端 coding agent**

Bunny Byte 跑在你的仓库里，连接 OpenAI-compatible、Anthropic-compatible 或 DeepSeek 等模型 provider，
通过受控工具读取代码、搜索、运行命令、修改文件，并把 session、事件流、trace、报告和长期记忆都保存在本地。

</div>

<p align="center">
  <img src="assets/screenshots/bunnybyte-tui-intro.png" alt="bunnybyte TUI 启动界面" width="960">
</p>

---

## Bunny Byte 是什么

Bunny Byte 是一个本地终端里的 coding agent。它不是把模型直接放到 shell 里裸跑，而是把一次任务拆成可观察、可恢复、可审计的运行链路：

- **Provider profile**：用 profile 区分模型来源，用 `protocol` 决定请求格式，支持 OpenAI Responses API 和 Anthropic Messages-compatible endpoint。
- **Prompt context**：把运行规则、仓库信息、可用工具、skills、工作记忆、长期记忆和最近历史按预算组装进 prompt。
- **文本协议工具调用**：模型输出 `<tool>...</tool>` 或 `<final>...</final>`，本地 parser 校验后才执行工具；短自然语言前缀会保留给用户看。
- **Tools**：文件列表、读文件、搜索、shell、写文件、精确 patch、todo、ask_user、子 agent、plan mode。
- **Approval / sandbox / policy**：写操作、shell 和风险动作可审批；`run_shell` 可用 bubblewrap 沙箱；工具策略会阻止越权和不合适的重复调用。
- **Subagents**：Explore 只读探索，worker 可按 write scope 执行局部任务；子 agent session 会标记为 internal，不混入普通历史列表。
- **Run evidence**：每轮都会保存 session JSON、events JSONL、run trace、task state、report 和 checkpoint。
- **Memory / auto-dream**：working memory 续接当前任务，daily logs 记录观察，durable topics 沉淀长期项目知识。

Bunny Byte 的目标是：**像本地工程工具一样透明，像 coding agent 一样能连续完成任务。**

> BunnyByte is AI and can make mistakes. Please double-check responses.

## 界面

TUI 和 REPL 使用同一个 runtime。TUI 里可以看到模型状态、工具调用、工具结果、worker 通知、slash command 补全、session 进度和上下文用量。

<p align="center">
  <strong>工具和子 agent</strong><br>
  <img src="assets/screenshots/bunnybyte-tui-tools.png" alt="bunnybyte TUI 工具调用和子 agent" width="960">
</p>

<p align="center">
  <strong>Skills、help 和命令补全</strong><br>
  <img src="assets/screenshots/bunnybyte-tui-skills-help.png" alt="bunnybyte TUI skills、help 和命令补全" width="960">
</p>

<p align="center">
  <strong>Memory 和 durable topics</strong><br>
  <img src="assets/screenshots/bunnybyte-tui-memory-skills.png" alt="bunnybyte TUI memory 和 skills" width="960">
</p>

<p align="center">
  <strong>Slash command 工作区</strong><br>
  <img src="assets/screenshots/bunnybyte-tui-latest.png" alt="bunnybyte TUI slash command 补全" width="960">
</p>

## 安装

要求：Python 3.10+、git，以及至少一个可用的模型 provider key。

### 从 GitHub 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/LISAooo1234/Bunny-Byte/main/install.sh | bash
```

这个脚本会把项目安装到 `~/.bunnybyte-agent`，创建独立虚拟环境，并在 `~/.local/bin` 写入全局命令：

```bash
bunny
bunnybyte
```

安装后不需要进入项目目录，也不需要用 `uv run`。在任意仓库里直接运行：

```bash
bunny setup
bunny
```

如果终端提示找不到 `bunny`，把 `~/.local/bin` 加入 `PATH` 后重新打开终端：

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

### pipx 安装

如果你习惯用 `pipx` 管理全局 Python CLI，也可以直接从 GitHub 安装：

```bash
pipx install git+https://github.com/LISAooo1234/Bunny-Byte.git
bunny
```

### 源码开发

```bash
git clone https://github.com/LISAooo1234/Bunny-Byte.git
cd Bunny-Byte
pip install -e ".[dev]"
```

开发 checkout 推荐使用 uv：

```bash
uv sync --dev
uv run bunny
```

安装后可用的入口：

```bash
bunny          # 推荐：任意目录启动 Bunny Byte
bunnybyte      # 完整命令名
bb             # 短别名
bunnybyte-tui  # 直接启动 TUI
bbtui          # TUI 短别名
```

## 配置 Provider

Bunny Byte 启动前会解析一个 **provider profile**。profile 名字用于选择配置；真正决定 HTTP 请求格式的是 `protocol`。

| 字段         | 作用                                             |
| ------------ | ------------------------------------------------ |
| `protocol` | 请求协议，目前支持 `openai` 和 `anthropic`。 |
| `api_key`  | 发给 provider 的 key。                           |
| `base_url` | provider endpoint。                              |
| `model`    | 本次请求使用的模型名。                           |

### 小白推荐：全局配置一次

第一次安装后运行：

```bash
bunny setup
```

它会引导你选择 provider、输入 API key，并把配置保存到：

```text
~/.config/bunnybyte/config.toml
```

之后不需要每个项目都配一遍。在任意仓库里直接运行：

```bash
bunny
```

也可以用一行命令直接写入全局配置：

```bash
bunny setup --provider deepseek --api-key sk-...
```

查看当前全局配置：

```bash
bunny config show
bunny config path
```

### 全局配置文件

`bunny setup` 生成的文件大致如下：

```toml
provider = "deepseek"

[providers.deepseek]
protocol = "anthropic"
api_key = "sk-..."
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-pro"
```

注意：`provider = "deepseek"` 只是选择 profile 名字；`protocol = "anthropic"` 才决定走 Anthropic-compatible Messages API。

配置合并优先级：

```text
CLI 参数 > 环境变量 > 项目 .bunnybyte.toml > 全局 ~/.config/bunnybyte/config.toml > 代码默认值
```

普通用户只需要全局配置。项目 `.bunnybyte.toml` 只适合某个仓库必须使用不同 provider 或不同模型时覆盖全局设置。

### 项目级覆盖（可选）

如果某个仓库需要单独配置，再放项目 `.bunnybyte.toml`：

```bash
cp .bunnybyte.toml.example .bunnybyte.toml
$EDITOR .bunnybyte.toml
```

`.bunnybyte.toml` 默认被 `.gitignore` 忽略，不要提交真实 key。

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

### OpenAI-compatible 中转

`protocol = "openai"` 当前走 OpenAI Responses API，请求路径是 `/v1/responses`。如果第三方中转只支持传统 `/v1/chat/completions`，可能出现空响应或格式不兼容；请确认中转支持 Responses API，并使用中转后台明确列出的模型名。

### 环境变量

```bash
export BUNNYBYTE_PROVIDER=deepseek
export DEEPSEEK_API_KEY=sk-...
export DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
export DEEPSEEK_MODEL=deepseek-v4-pro

bunny
```

常用变量：

| Provider             | 变量                                                               |
| -------------------- | ------------------------------------------------------------------ |
| DeepSeek             | `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`    |
| OpenAI-compatible    | `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL`          |
| Anthropic-compatible | `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` |
| 通用覆盖             | `BUNNYBYTE_API_KEY`, `BUNNYBYTE_BASE_URL`, `BUNNYBYTE_MODEL` |

更多配置见 [docs/configuration.md](docs/configuration.md)。

## 启动和常用参数

```bash
bunny                                  # 默认 Textual TUI
bunny --repl                           # 普通终端 REPL
bunny "找出测试失败的根因"              # one-shot 任务
bunny --resume latest                  # 续接最近普通 session
bunny --cwd /path/to/repo              # 指定工作目录
```

常用运行参数：

```bash
bunny --provider deepseek --model deepseek-v4-flash
bunny --approval ask                   # shell / 写文件前询问
bunny --approval auto                  # 普通操作自动通过
bunny --approval never                 # 非交互模式
bunny --sandbox best_effort            # 尽量隔离 shell 命令
bunny --no-auto-dream                  # 关闭后台 memory 整合
bunny --max-steps 80                   # 提高单次任务最大迭代步数
bunny --max-new-tokens 32000           # 可选：显式限制每步最大输出 token
```

`--max-new-tokens` **默认不设置上限**。Bunny Byte 默认不再主动给 provider 下发 `max_tokens` / `max_output_tokens`；只有显式传入该参数时才限制模型输出。

## 日常用法

进入 TUI 或 REPL 后可以直接输入自然语言，也可以用 slash command：

```text
> /help
> /skills
> 找出测试失败的根因
> /plan 重构 provider 配置加载逻辑
> /review
> /test tests/test_config.py
> /remember 这个项目用 DeepSeek 的 Anthropic-compatible endpoint
> /dream
```

常用命令：

| 命令                         | 作用                                                           |
| ---------------------------- | -------------------------------------------------------------- |
| `/help`                    | 查看内置命令。                                                 |
| `/skills`                  | 列出可用 skills。                                              |
| `/skill <name> [args]`     | 运行指定 skill。                                               |
| `/session`                 | 查看当前 session、events、run 路径。                           |
| `/history`                 | 列出历史普通 session（默认隐藏 worker / dream 内部 session）。 |
| `/resume latest`           | 续接最近普通 session。                                         |
| `/context`                 | 查看 prompt context 使用情况。                                 |
| `/usage`                   | 查看 provider、model、token 元数据。                           |
| `/memory`                  | 查看 durable memory 索引。                                     |
| `/working-memory`          | 查看当前 session 工作记忆。                                    |
| `/remember <text>`         | 保存一条 durable note 到 daily log。                           |
| `/dream`                   | 把 daily log 整合成 durable memory topics。                    |
| `/plan <topic>`            | 进入 plan mode。                                               |
| `/plan-exit`               | 退出 plan mode。                                               |
| `/agents`                  | 查看子 agent / worker 状态。                                   |
| `/subagent explore <task>` | 手动启动只读 Explore 子任务。                                  |
| `/model <name>`            | 当前 session 临时切模型。                                      |
| `/provider <name>`         | 当前 session 临时切 provider profile。                         |
| `/compact`                 | 压缩较早的对话历史。                                           |
| `/clear`                   | 开一个新的空 session。                                         |
| `/exit`                    | 退出 bunnybyte。                                               |

## 核心能力

| 能力                  | 说明                                                                              |
| --------------------- | --------------------------------------------------------------------------------- |
| TUI / REPL / one-shot | 同一个 runtime，不同入口。                                                        |
| 文本协议解析          | 支持 `<tool>` / `<final>`；可保留短 preamble；支持多工具块和部分格式恢复。    |
| 工具执行              | 文件列表、读文件、搜索、shell、写文件、patch、ask_user、todo、子 agent。          |
| Retry 修正            | 模型协议错误会作为当前 turn 的临时 correction 重试，不再长期污染普通 history。    |
| Plan mode             | 先读代码和拆计划，再进入执行阶段。                                                |
| 子 agent              | Explore 只读探索；worker 可按 write scope 处理局部任务；内部 session 默认隐藏。   |
| Skills                | 内置 `/review`、`/test`、`/commit`、`/simplify`，也支持用户和项目自定义。 |
| Memory                | working memory、daily logs、durable topics、auto-dream。                          |
| Evidence              | session JSON、event stream、run trace、task state、report、checkpoint。           |
| Sandbox               | 对 `run_shell` 做可选隔离。                                                     |

## 子 agent 和委派任务

Bunny Byte 可以把复杂任务拆给子 agent：

- **Explore**：只读探索，适合并行查找文件、理解架构、定位问题。
- **worker**：可写子任务，适合限定范围内实现、修复或补测试。

父 session 只保存 worker 状态和结果摘要；worker / dream 的内部 session 会带 `kind = "worker"` 或 `kind = "internal_dream"`，默认不会混入 `/history` 和 `/resume latest`。

## 本地文件

| 数据           | 路径                                                                |
| -------------- | ------------------------------------------------------------------- |
| 全局配置       | `~/.config/bunnybyte/config.toml`                                 |
| 项目级覆盖配置 | `.bunnybyte.toml`                                                 |
| 会话历史       | `.bunnybyte/sessions/<id>.json`                                   |
| 事件流         | `.bunnybyte/sessions/<id>.events.jsonl`                           |
| 运行证据       | `.bunnybyte/runs/<run_id>/`                                       |
| 记忆索引       | `.bunnybyte/memory/MEMORY.md`                                     |
| Daily logs     | `.bunnybyte/memory/logs/YYYY/MM/YYYY-MM-DD.md`                    |
| Durable topics | `.bunnybyte/memory/topics/*.md`                                   |
| 用户 skills    | `~/.bunnybyte/skills/<name>/SKILL.md`                             |
| 项目 skills    | `skills/<name>/SKILL.md` 或 `.bunnybyte/skills/<name>/SKILL.md` |
| 计划文件       | `.bunnybyte/plans/*.md`                                           |

## 项目结构

```text
bunnybyte/
├── cli.py                 # CLI 参数、启动模式、REPL 命令
├── commands/              # slash command 注册和解析
├── config/                # provider profile、TOML、env 解析
├── core/                  # runtime、engine、session、workers、context、evidence
├── features/              # memory、skills、sandbox
├── providers/             # OpenAI-compatible / Anthropic-compatible client
├── tools/                 # tool registry 和具体工具
├── tui/                   # Textual TUI
└── evaluation/            # run evidence、metrics、evaluation helpers
```

## 测试

```bash
pip install -e ".[dev]"
pytest tests/ -q

# 或 uv
uv sync --dev
uv run pytest tests/ -q

# 真实 provider 烟测需要 key
BUNNYBYTE_LIVE_SMOKE=1 pytest tests/test_release_smoke.py -q
```

## 文档

| 入口                                 | 内容                                                      |
| ------------------------------------ | --------------------------------------------------------- |
| [配置](docs/configuration.md)           | 全局 provider 配置、项目级覆盖、环境变量和 sandbox 配置。 |
| [分层记忆 + auto-dream](docs/memory.md) | working memory、daily logs、durable topics 和后台整合。   |
| [Skills](docs/skills.md)                | `SKILL.md` 目录结构、内置技能和自定义 workflow。        |
| [Sandbox](docs/sandbox.md)              | `run_shell` 隔离模式、backend 选择和文件系统边界。      |

## License

MIT
