# Changelog

## 未发布

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
