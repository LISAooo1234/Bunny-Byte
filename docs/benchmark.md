# 跑分基准（`bunny bench`）

`bunny bench` 用真实模型（默认 `deepseek` 配置）在一组固定的编码任务上给 Bunny Byte 打分。
每个任务都在全新的临时 git 仓库里运行，由 shell verifier 判定结果，而不是看模型自己说“做完了”。

```bash
bunny bench                      # 跑全部任务，结果写入 benchmarks/results/
bunny bench --list               # 列出任务
bunny bench --tasks add_cli_flag,multi_file_rename   # 部分任务默认不写结果文件，加 --save 才保存
bunny bench --repeat 3           # 每题跑 3 次，分数更稳
bunny bench --history            # 查看历史分数
bunny bench --provider openai --model gpt-x --no-save
bunny bench --json > result.json
```

注意：bench 会真实调用模型并产生费用（整轮 8 题约 20 万 token 量级，实际费用取决于所选模型的价格）。任务以自动批准模式运行在临时目录里，且不启用 sandbox，`run_shell` 直接在本机执行；请只在你信任所选模型的环境里运行。

## 任务集

| 任务 | 类别 | 考察点 |
| --- | --- | --- |
| `fix_failing_test` | 修 bug | 跑测试、定位 `//` 误用、不许改测试文件 |
| `recover_from_bad_patch` | 工具恢复 | 文件里有两处相同的 `timeout = 30`：补丁要带足上下文才能唯一定位，被拒后能自行恢复 |
| `fix_crash_from_traceback` | 修 bug | 读报错修字段名，并满足输出格式要求 |
| `implement_from_docstring` | 写功能 | 按 docstring（含 doctest）实现函数 |
| `multi_file_rename` | 重构 | 跨 5 个文件重命名，不误伤同前缀的另一个函数 |
| `add_cli_flag` | 写功能 | 给脚本加开关，并同步更新 README |
| `answer_codebase_question` | 只读问答 | 在有过时文档干扰的代码库里找答案，且不得改动任何文件 |
| `count_rows_to_file` | 数据处理 | 用命令统计，排除相似值 `failed_retry` |

判分规则：

- verifier 是 shell 命令；需要“隐藏测试”的任务，测试文件只在判分时写入，agent 运行期间看不到。
- `frozen` 文件（例如测试文件、输入数据）被改动即判失败；只读问答任务改动任何文件即判失败，且最终回答必须包含正确事实。
- agent 的步数硬上限与正常 CLI 一致（`--max-steps`，默认 50），所以通过率反映真实使用；每题另有一个“高效完成应有的步数”，超出只标记为 `over_budget`，不判负。
- 模型/网络故障、超过墙钟上限都算该题失败，不会中断整轮 bench。bench 不启用 `fallback`，避免备用模型污染分数。`--timeout` 到点会请求 agent 中止（agent 在步与步之间检查），再宽限 5 秒，仍不返回就放弃该线程并判失败。

## 输出指标

通过率、正常收尾率（模型自己给出最终回答，而不是撞到步数上限）、步数达标率、平均步数、平均模型调用数、token（provider 没有上报就显示“未上报”，不会写成 0）、缓存命中 token（Anthropic 风格下与输入分开计，OpenAI 风格下包含在输入里）、耗时。

每次完整运行会写一个 JSON 到 `benchmarks/results/`（在 Bunny Byte 源码目录里运行时；否则写到 `~/.bunnybyte/bench-results/`，也可用 `--out` 指定），包含版本、git 提交、模型、任务集指纹和逐题明细；失败原因中的临时目录和用户目录会被替换为 `<repo>` / `~`，不含密钥。
只有“任务集指纹、provider、模型、步数上限”都相同的结果才会互相比较并显示变化量。

## 如何解读

- 任务只有 8 道，**每题 = 12.5 个百分点**；单次结果上下浮动 1 题属于噪声。看趋势请用 `--repeat` 或对比多条历史。
- 通过率接近满分时，真正有区分度的是平均步数、token 与正常收尾率：它们能直接度量“模型做完了却停不下来”这类浪费。
- 改动任务内容会改变任务集指纹，旧结果不再参与比较。

## 离线自检

`tests/test_bench.py` 不调用任何模型：每个任务都带一份 oracle 参考解（脚本化的工具调用序列），测试断言

1. verifier 在原始 fixture 上**失败**（任务不会不做事就通过），
2. oracle 参考解经过真实 agent 循环与工具层之后 verifier **通过**（verifier 不会误杀正确答案），
3. 改动 frozen 文件、只读任务写文件、回答错误、超时、模型异常等情况都按预期判负。
