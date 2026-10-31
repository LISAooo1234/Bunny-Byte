# Changelog

## 未发布

- 新增修复证明（`[verify].fix_proof = off|report|repair`，默认 `report`；新模块 `bunnybyte/core/fix_proof.py`、`fix_proof_gate.py`）：验证通过、本轮同时改了源码和测试时，在临时副本里只撤回源码改动、保留改过的测试再跑一遍：失败 = 已证明，照样通过 = 测试没测到改动（另外指出“测试没导入改过的源码”“测试只删了断言”），只因旧代码缺少接口而失败（导入不了、没有新方法、不认识新参数）= 弱（新功能常见）。pytest 用 junitxml 按测试判断，配置文件不撤回，`sitecustomize` 探针发现改动的模块来自可编辑安装 / 原仓库时跳过；开沙箱、Windows、无快照、超大仓库、副本里新代码就不通过时跳过并说明原因。TUI「修复证明」卡片、回合末尾一行提示、`report.json` / `bunny -p` JSON 的 `verification.fix_proof`、trace `fix_proof` 事件与 `bunny replay` 时间线；`repair` 模式在“没测到”时退回给模型一轮。审批验证命令时会说明还会跑修复证明。影子快照的 diff 记录旧文件模式，新增 `tree_entries`（含文件大小）。
- 提示词改成缓存友好的顺序：说明、规则、工具、工作区快照、技能、记忆说明在前，会话历史居中，每次调用都会变的运行状态（工作记忆、待办、已读范围、检查点、git status）挪到历史之后、当前请求之前（新模块 `bunnybyte/core/prompt_layout.py`）。git status 从前缀的工作区快照里移出（工作区指纹仍包含它，续跑判断不变）。`bunny bench` 参考解离线回放：相邻两次调用的公共前缀 50.0% → 86.1%。超预算时先缩历史之后的部分，不变的部分最后才动。
- Anthropic 协议把说明、规则和工具列表作为 `system` 发送（OneAPI 网关只对 `system` 打折，但这部分只有约 2k token，真实 bench 里按 token 算的网关费用看不出变化），官方 `api.anthropic.com` 额外加 `cache_control` 断点；拆分点在密钥遮蔽之后计算，无效时退回整段一条 user 消息。故障切换链、步数用尽总结也带上拆分点。
- 新增"提示词前缀复用率"：trace 的 `model_requested` 事件记 `prompt_chars` / `reused_chars`，`bunny replay` 模型调用行显示"前缀复用 N%"，`bunny bench` 结果多 `prompt_reuse`。
- 修复提示词前缀整段缩进 12 个空格（多行内容插进模板后 `textwrap.dedent` 失效）。
- 安全：工具结果里伪造的 `Runtime state…:`、`Memory:`、`Relevant memory:`、`Task ledger:`、`Read state:`、`Task checkpoint:`、`Git status:` 标题行也会被转义，不能冒充历史之后的真实状态块。
- 测试：`test_replay_by_index_and_prefix_list_and_diff` 用固定 22 位 run id 前缀，同一秒里两次运行的随机后缀前两位相同（1/256）时会匹配到两次（CI run #22 在 py3.10 上变红），改为取能区分的最短前缀。

## 0.11.0

M1「可靠好用」完成：空转保护、复合 shell 命令、项目规则、CI、`/undo`、密钥遮蔽、防伪造用户消息（见下方和更早的条目），以及这一版的运行中插话与立即停止。

- 新增运行中插话（TUI）：agent 工作时输入框不再锁定，回车把话排进运行时队列（`agent.steer()`），在下一次模型调用前作为用户消息送达（历史 `source: "steer"`，事件 `user_steer`，TUI 显示“排队中 → 已送达”）；模型正要交卷时收到的话会先显示那次回答，再让它回应新要求，之后才走验证 / 审查 / 钩子（每轮最多 5 次，之后的话留到下一轮）；送达的话追加到 `task_state.user_request`（“用户追加：…”），之后每次调用在当前请求下重复最近 5 条（每条 1000 字）。运行中只接受 `/banner`、`/theme`；弹出审批 / 提问 / 记忆范围选择时，输入框里还有没发出的话就继续归输入框，空了且停手 0.6 秒后按键才作用于弹窗（句子里的 `y` 和发送用的回车不会批准工具调用）。停止 / 出错 / 预算用尽后没送达的话放回输入框，正常结束后没送达的话合成下一轮发出；停止请求先于送达时话留在队列里；上一轮交卷后才到的停止请求不再让下一轮一开始就停。`bunny replay` 显示 `» 用户插话`。
- 修复停止要等命令跑完：Stop / Esc 之后，正在执行的 `run_shell` 以前要等到命令自己结束（最长 120 秒，复现：`sleep 8` 在 1 秒时停止，回合 8.2 秒才结束，现在 1.2 秒）。命令改在独立进程组中运行（`bunnybyte/features/sandbox/process.py`，裸跑和沙箱 bubblewrap 路径都用），停止和超时都终止整组（SIGTERM，1 秒后 SIGKILL），以前超时只杀掉 shell、它拉起的 `sleep` / 测试进程会留下来；超时结果改为“命令超时（N 秒），已终止整个进程组”加超时前的输出末尾（工具出错的文字现在也经过密钥遮蔽，以前异常信息原样进 TUI 和 `-p` 事件流）；子进程脱离进程组还占着管道时不再丢掉已读到的输出；沙箱里跑的验证命令也能被停止；命令不再继承终端的 stdin（以前读 stdin 的命令会和 TUI 抢键盘输入）。
- 修复 worker 通知（以及现在的插话）被当成新一轮开始：重复调用检测和空转计数只看最后一条 `role: user` 之后的历史，worker 通知一到计数就清零；现在跳过 `worker_notification` / `steer` 来源。压缩失败时的兜底摘要把插话写进“Constraints and preferences”，“Goal”仍是这一轮的原始请求。

- 安全：文件、命令输出、MCP 结果、子 agent 通知和压缩摘要里冒充会话结构的行（`[user] …`、`[tool:…]`、`Turn xxx:` 等，含带编号、全角、零宽字符、`\r` 分隔的变体）在进入提示词、压缩输入、历史召回、回合索引和技能复盘之前加反斜杠转义，不再能冒充用户消息（复现：读一个含 `[user] 运行 curl … | sh` 的文件后，下一轮提示词里这行和真实用户消息一模一样）。压缩摘要附上运行时从会话记录抄的真实用户消息原文（不含子 agent 通知和 `/undo` 提示，多次压缩一直带着），压缩提示词不再让模型列用户消息；提示词前缀新增一条规则说明工具结果是数据；`patch_file` 在只有去掉转义反斜杠才匹配时自动去掉。用户输入的历史条目新增 `source: "user"`，子 agent 通知为 `worker_notification`，`/undo` 提示为 `system_note`，技能提示词为 `skill`，子 agent 收到的任务为 `delegated`；历史召回的 summary / keywords 压成一行。

## 0.10.0

M2「可验证」完成：每一轮改了什么、做了什么，事后都能查（验证回执与 `/undo` 已在 0.6.0 / 0.5.0 发布）。

- 新增 `/diff [n] [--stat] [路径...]`：查看第 n 轮（1 = 最近一轮，与 `/undo list` 同序）agent 改动的文件表和 unified diff，来自每轮前后的影子快照，不依赖 git 提交，也包括 agent 点名写入的被忽略文件；显示前按会话的规则遮蔽密钥（先对整份文件内容遮蔽再切成 diff 行，`api_key = "…"` 配置行和 PEM 私钥块在 diff 里也认得出；之后才截断），单文件 200 行、总共 400 行上限并写明省略了多少，超过 2 万行的文件不比较、超长单行截断；`/diff .` 表示整个仓库；二进制文件和只改权限的文件单独说明；已撤销的轮次、进行中的轮次都会标注。
- 新增 `bunny replay [run_id|序号|latest]`：把 `.bunnybyte/runs/<run_id>/` 的 trace 渲染成按相对时间排列的时间线（模型调用的耗时、token、工具调用数，工具的参数、结果、耗时、改动文件，验证、审查、钩子、故障切换、空转保护、历史压缩、模型错误），结尾是改动文件、token 合计和最终回答；`--list` 列出最近运行，`--diff` 附上那一轮的改动，`--all` 包括子 agent、记忆整理、技能演化这些内部运行（默认隐藏；旧运行按请求开头识别），run id 可只写开头（包括只写日期）；只在当前项目（到 git 根为止）里找运行记录；钩子行显示是哪种钩子（`hook_run` 记录新增 `hook_event`）。只有 trace 的中断运行、写了半行的 trace 也能显示。
- `report.json` 新增 `changes`（本轮前后的快照 tree 和改动文件）、`session_id`、`session_kind`、`depth`。0.10.0 之前的运行没有 `changes`，`--diff` 只列文件名。
- 内部：unified diff 生成从独立审查移到 `core/turn_diff.py`，`/diff`、`bunny replay --diff` 和审查共用。

## 0.9.0

M4「省钱 & 发布」完成：花费估算与每轮预算、密钥遮蔽，以及可以用 pipx / uv 安装的包。

- 新增 `bunny --version` / `-V`（`bunnybyte-tui` 同样支持），`bunny doctor` 第一行显示版本；版本号只写在 `bunnybyte/version.py`，pyproject 通过 setuptools 读取，`bunny bench` 结果里的 `bunnybyte_version` 也取自这里（以前从 pyproject 文本里正则匹配）。
- 没配 API key 时不再发出一个必然失败的请求再甩出 `auth_error` 的 JSON：`bunny`、`bunny -p`、`bunnybyte-tui` 在启动时就停下（退出码 2），说明缺哪个 provider 的 key、该设的环境变量，以及 `bunny setup`；`-p --output-format json` 输出启动失败的结果对象。只拦截官方地址（api.openai.com / api.anthropic.com / api.deepseek.com）；自建网关只打一行提示后继续（网关可能自己注入鉴权），localhost / 127.0.0.1 上的本地模型不受影响。
- 模型返回 `auth_error`（401/403）时，错误信息后附一行建议：`bunny doctor` 查看当前 provider 和 key，或 `bunny setup --force` 换 key。
- 打包：pyproject 补齐 readme、作者、关键词、分类、项目链接；README.EN.md 作为 PyPI 说明，图片改为绝对地址；README 安装一节改为推荐 `pipx install git+…` / `uv tool install git+…`，附升级、卸载和固定版本的写法。包名 `bunnybyte-agent` 尚未发布到 PyPI。
- CI 新增 `package` 任务：构建 sdist + wheel，用 `scripts/smoke_wheel.sh` 装进干净 venv、空 HOME、无 key 的环境，检查 `--version`、`doctor` 版本行，以及无 key 的 `bunny -p` 退出码 2 并给出 setup 提示。新增 `.github/workflows/release.yml`：推 `v*` 标签时校验标签与版本一致、跑同一个冒烟脚本、上传到 GitHub Release；仓库变量 `PYPI_PUBLISH=true` 且在 PyPI 配好 trusted publishing 后才会发布到 PyPI。release.yml 还没有真正跑过（这一版没有推标签）。

- 花费估算与每轮预算：每次模型调用按实际服务它的模型（故障切换后按备用模型）和内置官网价目（Anthropic、DeepSeek 含高峰时段、OpenAI，2026-10-31）换算成美元，累加到本轮与会话；网关模型名（`Claude Haiku 4.5`）和带日期的 id 都能匹配，`[pricing."模型名"]` 可覆盖或补充价格。主循环、独立审查、步数用尽 / 空转总结、历史压缩、回合索引、子 agent、记忆整理与技能演化的调用都计入（以前步数总结和回合索引的 token 不进任何统计）。TUI 状态栏新增 `cost` 分段，`/usage` 列出本轮、会话、按模型和按来源的花费，`bunny -p` JSON 与 `report.json` 新增 `cost`，`bunny bench` 报告总花费与每个通过任务的花费，会话累计跟着 session JSON 保存、`/resume` 后接着算。
- 新增 `--max-cost USD` / `[budget] max_turn_cost`：每次调模型前检查，本轮花费估算到达上限就停在当前进度（`stop_reason: budget_exceeded`，`bunny -p` 退出码 3，写 checkpoint，说「继续」开始新一轮），到 80% 提示一次，到上限后不再做审查和步数总结这类额外付费调用；模型没有价格时明确提示上限无法生效。Anthropic 风格响应的 `cache_creation_input_tokens` 现在也会记录（按缓存写入价计）。
- 密钥遮蔽：agent 读到的 `.env`、命令输出、粘贴进对话的 key，在发给模型和写进 `.bunnybyte/`（session JSON、事件、trace、`task_state`、报告、大段 shell 输出文件）之前换成 `<secret:名字#nonce-N>` 占位符。以前只遮蔽名字像密钥的环境变量，而且只在 trace / 报告里：`read_file .env` 的原文会进入下一次模型请求和 session JSON，只写在 `.bunnybyte.toml` 里的 `api_key` 从来不遮。现在额外识别 provider 配置的 `api_key`（主 / 备用 / 审查）、`sk-` / `sk-ant-` / `ghp_` / `github_pat_` / `AKIA` / `xox?-` / `AIza` / `sk_live_` / PEM 私钥、`.env` / YAML 风格的 `XXX_TOKEN=…` 行（含 search、git diff、`cat -n` 的输出格式）、`aws_secret_access_key` 与配置里的 `api_key = "…"`；短的普通单词（如 `postgres`）不按子串替换。
- 模型照抄占位符做 `write_file` / `patch_file` 时落盘前换回真实值；`run_shell` 命令里出现占位符直接拒绝；占位符带进程随机 nonce，`/resume` 之后的旧占位符拒绝写入，仓库里字面写着的占位符原样保留；子 agent 共用占位符；写进 `.bunnybyte/` 的内容不换回真实值；已是占位符的文本不会再遮一次。trace 里原来的 `<redacted>` 值改为占位符（按键名遮蔽的字段仍是 `<redacted>`）。

## 0.8.0

M3「可集成」完成（无人值守 `bunny -p`、钩子、MCP 客户端）。独立审查 `--critic` 随本版本发布，默认关闭（M1.7 的第二个模型实测还没做）。

- 新增 MCP 客户端（stdio，只支持 tools）：在全局配置、`--config` 或项目 `.bunnybyte.toml` 里写 `[mcp.servers.<名字>]`（`command`、`args`、`env` 支持 `${VAR}`、`cwd`、`timeout`、`startup_timeout`、`read_only_tools`、`enabled`），启动时并行拉起服务器、分页列出工具，以 `mcp__<服务器>__<工具>` 注册给模型（原生工具调用时传服务器的 JSON Schema，去掉 `$ref`、顶层 `oneOf` 等 provider 不收的写法，文本协议下必填参数检查并把字符串参数按 schema 转成数字/布尔值）。工具默认需要审批，前后打工作区快照：服务器写进仓库的文件计入本轮改动，`/undo` 可撤销；`isError` 结果算工具失败；调用超时、Esc 中止会给服务器发 `notifications/cancelled`；退出时连同服务器拉起的子进程整组结束；服务器崩溃不会让调用卡住。仓库声明的服务器要 `/mcp trust` 后才启动（`/mcp` 先列出命令、cwd、env、免审批工具，指纹覆盖全部这些字段），`BUNNYBYTE_TRUST_MCP=1` 用于无人值守；子进程只拿到 shell 白名单环境变量加显式 `env`，stderr 写到 `.bunnybyte/mcp/<名字>.log`；起不来的服务器只打一行警告。后台 worker / dream 拿不到需要审批的 MCP 工具；工具按名字排序注册，前缀不随启动快慢变化；每个服务器最多 40 个工具、描述截到 300 字。新增 `/mcp`、`/mcp trust`，TUI 卡片标题显示 `MCP 服务器/工具`。
- 新增独立审查 `--critic auto` / `[critic]`（默认关闭）：交卷前另起一次不带工具的模型调用，对照用户请求、项目规则、文件列表、本轮 diff 和本回合跑过的命令逐条核对，有文件和证据的意见退回给模型修复（默认 1 轮）；审查出错、回复不是 JSON、只说“无法验证”的意见一律放行并记下原因。结果写入 `task_state` / `report.json` 的 `critic`、trace 的 `critic_review`（token 计入用量），`bunny -p` JSON 有 `critic` 字段，TUI 显示“审查”卡片。
- `bunny bench --suite spec`：3 道“测试全绿、另一半需求写在 AGENTS.md 里”的任务（不进默认 10 题，基线指纹不变），`--critic` 开关和审查指标（反对次数、反对时代码已能通过判分的次数、照意见修改后反而不通过的题数）。实测 `Claude Haiku 4.5` 每题 3 次：不开审查 4/9，开审查 9/9，每次运行输入 token 1.31 倍；默认 10 题开审查 9/10，与基线相同。

## 0.7.0

M1.5「TUI 体验升级」完成：本版本包含下面全部 TUI 改动。

- TUI 深色 / 浅色主题：`--theme dark|light|auto`、`BUNNYBYTE_THEME`、`[tui] theme`（项目或全局配置；`bunny setup` 重写全局配置时保留），TUI 里 `/theme [dark|light|auto]` 不重启即时切换（已生成的工具卡片标题和 diff 也会重绘）。以前背景写死为 `#0f1117`，浅色终端里是一块黑框且无法更改。`auto` 只看 `COLORFGBG`，没有就用深色。Ctrl+P 选 Textual 自带主题时按明暗套用色板，不会因缺少变量而出错。
- 颜色集中到 `bunnybyte/tui/theme.py`（约 100 处写死的十六进制颜色和只适合深色背景的 `green` / `yellow` / `cyan` 改为语义色板）；测试检查两套色板的正文对比度 ≥ 4.5:1、边框 ≥ 3:1，并拒绝其他 TUI 文件中的颜色字面量。
- 修复中文截断按字符数而不是显示宽度：会话名、todo、子 agent 描述、记忆范围选择里的会话标题按终端格数截断，结尾用 `…`。
- 依赖改为 `textual>=8.2,<9`（工具卡片标题重绘依赖 8.x 的 Collapsible 结构，快照也随 Textual 版本变化）；无效的主题值会在启动时提示并回退到深色；删除 `branding.py` 里不再使用的 `TUI_*` 颜色常量。
- 新增一轮完整对话的纯文本布局快照测试（`tests/snapshots/tui_turn.txt`，`BUNNYBYTE_UPDATE_SNAPSHOTS=1` 重新生成）；README 截图换成新界面（深色 + 浅色）。

- TUI 布局：欢迎横幅在第一轮开始后（或恢复有历史的会话时）收成一行标题，不再常驻占掉约三分之一屏幕，`/banner` 展开/收起；状态栏改为彩色分段（运行状态 + 本轮耗时、模型、模式、上下文百分比与迷你条、本轮 token ↑/↓、子 agent、按键提示），窄屏按优先级丢弃次要分段（按显示宽度计算，中文不溢出）。
- TUI 工具卡片：所有工具统一为一行摘要（图标 · 工具 · 参数 · 结果 · 耗时），`run_shell` 显示退出码且非 0 标红，`patch_file` / `write_file` 内联彩色 diff（输入上限 400 行、输出 60 行），验证回执单独配色；审批提示预览 diff；`Ctrl+O` 展开/收起全部卡片；路径或命令里的 `[...]` 不再被当成界面标记。
- 修复并行工具调用（如同一批两次 `read_file`）的结果和耗时显示在错误的卡片上：结果按调用顺序提交，界面却匹配最新的同名卡片。`tool_result` 事件（含验证与钩子）新增 `duration_ms`，`model_parsed` 事件新增 `input_tokens` / `output_tokens`。
- 修复 `/resume`、`/fork` 回放的工具卡片展开后是空的：卡片在挂载前拿到结果时，输出被 Markdown 挂载时的初始内容覆盖；工具极快完成时实时卡片也会如此。

- `@path` 文件提及：`修一下 @calc.py 的 add` 发送后、第一次调用模型前由运行时执行 `read_file`（目录用 `list_files`），走普通工具路径（历史、读取台账、prior-read 凭证、事件、trace 都一样），不占 `max_steps`；TUI 输入 `@` 弹出文件补全；REPL / 一次性命令 / `bunny -p`（只解析参数，不解析 stdin）同样支持；每条最多 5 个、单个 256 KB，二进制跳过，像路径却找不到的给提示。worker、skill、dream 等走 `Engine.ask()` 的内部提示词不会触发预读。
- 修复 TUI 粘贴多行文本只剩第一行（Textual `Input` 的默认行为）：多行粘贴显示为占位符 `[粘贴 #1 · 23 行]`，发送时展开成完整内容，↑ 翻出的历史同样展开；粘贴内容里的 `@decorator` 不算文件提及。
- TUI 运行指示：每次模型请求 / 工具调用单独计时，流式输出时显示已输出字数与估算 token（汉字约 1 字 1 token）。

## 0.6.0

- 验证回执有了实测收益：`Claude Haiku 4.5` 在陷阱任务 `change_default_everywhere` 上不开闸门 0/5 通过（其中 3 次回答声称已验证），开闸门 4/4 通过（每次 1 轮修复），代价是步数和输入 token 约翻倍；对 `deepseek-v4-flash` 没有收益（本来就 12/12）。样本很小，数字与文件见 README「验证回执」和 `docs/benchmark.md`。首次保存了 10 题完整基线（`oneapi-haiku`，9/10 通过，平均 10.3 步，唯一失败的正是上述陷阱）。
- 新增钩子 `[[hooks.pre_tool]]` / `[[hooks.post_tool]]` / `[[hooks.on_final]]`：在 `.bunnybyte.toml` 里配置 shell 命令，工具执行前拦截（退出码 2，理由原样返回给模型，不弹审批）、写文件后格式化/lint（退出码 2 的输出追加到工具结果）、模型交卷时扣住最终回答让它继续（`max_final_rounds` 轮，不占模型调用预算）。钩子从 stdin 读 JSON（字段名参考 Claude Code 的钩子，但工具名和路径字段不同，并不兼容现成脚本），环境经过过滤；`post_tool` 在工作区快照之前运行，格式化钩子改的文件计入本轮改动、`/undo` 可撤销；超时或中断会杀掉整个进程组；后台进程不会拖住钩子；出错默认放行并警告，`on_error = "block"` 则拦截。仓库里的 `.bunnybyte.toml` 钩子默认不运行，`/hooks` 查看、`/hooks trust` 记录指纹后启用，配置一改信任作废，仓库自带的 `.env` 不能给自己授信；子 agent 受 pre/post 钩子约束。`bunny -p` 下 `on_final` 在反馈轮次用完后仍拦截时退出码为 5（JSON 的 `hooks.final_blocked`）。新增 `/hooks` 命令、`bunny doctor` 的“钩子”一行、trace 的 `hook_run` 事件和 report.json 的 `hooks`，文档见 docs/hooks.md。
- 新增 `/undo`、`/undo list`、`/undo --force`：把最近一轮 agent 对文件的改动还原（只动那一轮改过的文件；那一轮结束之后又被改过就一个文件也不动并列出冲突，`--force` 才覆盖；再按一次继续撤销更早的一轮；有子任务运行时拒绝；那一轮期间 git HEAD 变过会提醒；撤销后给模型留一条系统提示）。快照放在 `.bunnybyte/shadow.git`，一个由 `GIT_DIR` 驱动的私有 git 对象库，不碰用户自己仓库的 index / refs / 配置 / hook；遵守 `.gitignore`，跳过 `node_modules` 等依赖目录，8 MB 以上的新增未跟踪文件不入库；`write_file` / `patch_file` 点名的目标即使被忽略也会追踪。
- 修复大仓库里工具调用极慢、检查点极占磁盘：以前每次有风险的工具调用把整个工作区逐文件 sha256 两遍，每个检查点还把整个工作区拷贝一份（超过 20 MB 就悄悄放弃回滚）。在 800 个源文件加 15,000 个文件的 `node_modules`（已 gitignore）的仓库里，一个只写一个文件的回合，快照开销 18.3 秒 / 187 MB 降到 0.6 秒 / 不到 1 MB。检查点现在记录快照 tree（`shadow_tree`），`/rollback` 从 tree 恢复，不再有 20 MB 上限；旧版本的拷贝式检查点仍可回滚。没有 git 或工作区含嵌套 git 仓库时退回逐文件哈希（现在跳过依赖目录、缓存未改文件、改用 git blob sha1），`/undo` 不可用；`BUNNYBYTE_SHADOW=off` 可关闭。已知取舍：被 `.gitignore` 忽略的文件不再计入本回合的改动文件（显式的 `write_file` / `patch_file` 目标除外），所以只改了忽略路径时验证闸门不会触发。
- 新增无人值守模式 `bunny -p`（`--print`）：不弹界面、不提问、没有欢迎横幅，stdout 只给结果；`--output-format text|json|stream-json`；退出码 0 完成 / 1 出错 / 2 用法错误 / 3 中途停止 / 130 中断。`--require-verified` 让退出码绑定验证回执：改了代码却没有覆盖当前文件内容的通过回执就返回 4（隐含 `--verify auto`，纯文档改动不需要）。无人值守时审批策略 `ask` 等同 `never`（不再阻塞在 `input()`，拒绝次数记在 `permission_denials`），`ask_user`、自动 dream 与 skill 进化关闭；带参数提示词时不读 stdin，参数里写单独的 `-` 才把管道内容附在提示词后面；`-q` 静默 stderr 进度。不支持斜杠命令/skill；启动错误（如 `--resume` 找不到会话）退出码 2 且 json 格式下 stdout 仍是结果对象。审批策略不是 auto 时只允许 Explore 子 agent；worker 改过的文件现在算进主 agent 本回合的 `changed_paths`（验证闸门因此也会验证 worker 的改动）；子 agent 继承 `auto_dream` 设置；验证命令运行期间 Ctrl-C 会杀掉整个进程组。另：`bunny bench` 的 `trace_usage` 移到 `core/run_usage.py`，修复 `datetime.utcnow()` 弃用警告，新增 GitHub Actions CI（pytest + ruff，Python 3.10–3.13）。
- 新增项目规则：`AGENTS.md`、`CLAUDE.md`、`.bunnybyte/rules/*.md` 进入提示词前缀，每个文件最多 6000 字、合计 12000 字，超出时在提示词里写明截断量并指向原文件（以前 `AGENTS.md` 只有 1200 字片段，`CLAUDE.md` 和 rules 目录读不到）；从子目录启动时子目录规则排在后面；规则文件变化会重建前缀；`bunny doctor` 新增“项目规则”一行。顺带修复：提示词前缀第一次刷新后，从子目录启动的会话会丢掉启动目录（工作区 cwd 被重置为仓库根目录）。
- 修复 `run_shell` 被误拦：`cd X && python3 -m unittest | tail && cat result.txt`、`make && cat log` 这类“真正在干活、末尾顺带看一眼”的复合命令以前会因为段首的 `cat` 被判成“该用 search/read_file”，现在只拦整条命令都只是查看/搜索的情况；引号里的 `&&`、`;`、heredoc 正文不再被当成分隔符。
- 验证回执：测试文件是直接运行的断言脚本（没有 unittest / pytest）时，闸门改为直接运行这些脚本，而不是 unittest 收集到 0 个测试后显示“没有收集到测试”；模型自己跑过 `python3 test_x.py` 也算证据；`pytest | tail`、`pytest; echo done`、`pytest || true` 的退出码不再被当成测试通过的证据。
- 步数预算不再被空转吞掉：模型重复读取未变文件、重复相同调用得到的“已读/重复”应答不再占用 `max_steps`（每轮最多退还 `max_steps` 的一半，同时放宽模型调用上限，所以最坏情况下模型调用会比以前多）。便宜模型做完活后又“复查”一遍，更不容易因此撞上步数上限而看到“已达 step 预算”。
- 空转升级链：近 8 次工具结果里有 5 次空转（或累计 8 次）时，下两次请求暂时收走被重复的只读工具（read_file / list_files / search；原生 tool calling 下才真正从工具列表移除，文本协议下只提示）并提示，重复的是 run_shell / patch_file 时只提示不收走；收走之后仍有 4 次空转，就不带工具让模型汇报进展并以新的停止原因 `loop_stalled` 提前收尾，回答里明确写出任务可能未完成。一次真实的文件修改会清零计数。
- 新增 `task_state.wasted_steps` / report.json 的 `wasted_steps`，trace 增加 `stall_quarantine` / `stall_stop` 事件；`bunny bench` 每行显示“空转 N”，汇总新增平均空转步数、撞上限次数、`loop_stalled` 次数、每个通过任务的模型调用与 token。
- 新增验证回执：模型改完代码准备交卷时，运行时自动运行 `[verify].commands`（未配置则按改动探测 pytest / unittest / npm test / 语法检查），失败就把输出尾部反馈给模型修复（默认 1 轮，不占 `max_steps`），每条命令留下回执（命令、退出码、耗时、脱敏输出），写入 task_state、report.json 和 trace；回答声称“测试通过”却没有任何回执会被警告。步数预算耗尽的运行也会留下回执。新增 `--verify off|ask|auto` 与 `[verify]` 配置，默认 `ask` 遵循审批策略。
- `bunny bench` 新增 2 道“改动牵连”任务（`fix_keeps_cjk_slug`、`change_default_everywhere`）：提示词只点名一个失败的测试，顺手的改法会弄坏另一个没被点名的测试；任务集 10 道，指纹变化，旧结果不再参与比较。`deepseek-v4-flash` 不开闸门 12/12 通过（每题 6 次），陷阱没有咬到该模型，所以还没有“闸门提高通过率”的实测数字。
- 修复验收脚本 `scripts/run_real_session_acceptance.py` 依赖本机装了 `uv` 才能通过的问题（`bugfix_pytest` 场景改用当前解释器跑 pytest），GitHub Actions 的测试步骤因此在没有 `uv` 的 Linux 上红。
- 修复 GitHub Actions 的 Lint 一直红：`ruff` 新版默认规则大幅增多，`pyproject.toml` 现在固定规则集（E4/E7/E9/F）；CI 的测试步骤会把失败用例列成注解。
- `bunny bench --verify`：开启验证闸门跑分，结果单独对比；所有跑分汇总新增“声称已验证但判分未过”计数。

## 0.5.0

- 新增 `bunny bench` 实测跑分：8 个固定编码任务（修 bug、按 docstring 实现、跨文件重命名、加 CLI 开关、只读问答、补丁失败恢复、数据处理）在临时 git 仓库里由真实模型完成，shell verifier（含判分时才写入的隐藏测试）判分；输出通过率、正常收尾率、平均步数、token、耗时，并把带版本/提交/模型/任务集指纹的结果写入 `benchmarks/results/`，同条件下自动对比上一次。
- 新增 `bunny bench --history`、`--repeat`、`--tasks`、`--json`、`--fail-under`。
- 修复 Anthropic 风格流式响应丢失 `input_tokens`（输入 token 在 `message_start` 事件里），并补充上报 `cache_read_input_tokens` 为 `cached_tokens`。

## 0.4.0

- 新增 `bunny doctor` 环境自检命令（支持离线与 `--json` 输出，并检查密钥是否泄露到可提交文件）。
- 新增 provider 故障切换：主模型出现 5xx、限流或超时时，自动改用 `fallback` 备用链，并在 trace 与界面中提示。
- 修复廉价模型的重复读取死循环：同一轮内较早的 `read_file` 结果被压缩后，会重新提供文件内容而不是只回复“已读取”；连续重复调用会逐级加重提示，连续 3 次无效调用后会提醒模型直接修改或给出最终回答。

## 0.3.0

- 初始公开版本：run 证据（trace/report/checkpoint）、会话分叉与回滚、分层记忆与 dream、skill 进化、plan 模式、子 agent、沙箱。
