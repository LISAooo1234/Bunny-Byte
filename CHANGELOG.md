# Changelog

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
