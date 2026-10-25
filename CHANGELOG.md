# Changelog

## 未发布

- 步数预算不再被空转吞掉：模型重复读取未变文件、重复相同调用得到的“已读/重复”应答不再占用 `max_steps`（每轮最多退还 `max_steps` 的一半，同时放宽模型调用上限，所以最坏情况下模型调用会比以前多）。便宜模型做完活后又“复查”一遍，更不容易因此撞上步数上限而看到“已达 step 预算”。
- 空转升级链：近 8 次工具结果里有 5 次空转（或累计 8 次）时，下两次请求暂时收走被重复的只读工具（read_file / list_files / search；原生 tool calling 下才真正从工具列表移除，文本协议下只提示）并提示，重复的是 run_shell / patch_file 时只提示不收走；收走之后仍有 4 次空转，就不带工具让模型汇报进展并以新的停止原因 `loop_stalled` 提前收尾，回答里明确写出任务可能未完成。一次真实的文件修改会清零计数。
- 新增 `task_state.wasted_steps` / report.json 的 `wasted_steps`，trace 增加 `stall_quarantine` / `stall_stop` 事件；`bunny bench` 每行显示“空转 N”，汇总新增平均空转步数、撞上限次数、`loop_stalled` 次数、每个通过任务的模型调用与 token。
- 新增验证回执：模型改完代码准备交卷时，运行时自动运行 `[verify].commands`（未配置则按改动探测 pytest / unittest / npm test / 语法检查），失败就把输出尾部反馈给模型修复（默认 1 轮，不占 `max_steps`），每条命令留下回执（命令、退出码、耗时、脱敏输出），写入 task_state、report.json 和 trace；回答声称“测试通过”却没有任何回执会被警告。步数预算耗尽的运行也会留下回执。新增 `--verify off|ask|auto` 与 `[verify]` 配置，默认 `ask` 遵循审批策略。
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
