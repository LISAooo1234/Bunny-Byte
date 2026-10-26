# 钩子（hooks）

钩子是你自己写的 shell 命令，Bunny Byte 在固定的时机替你运行：工具调用之前（`pre_tool`）、之后（`post_tool`）、模型想交卷的时候（`on_final`）。
用它们可以把团队规矩变成**运行时强制执行**的东西，而不是写在提示词里祈祷模型照做：禁止碰某些文件、改完自动格式化并把 lint 报错塞回给模型、交卷前必须满足某个条件。

## 配置

写在项目的 `.bunnybyte.toml` 或全局的 `~/.config/bunnybyte/config.toml` 里：

```toml
[hooks]
max_final_rounds = 2          # on_final 最多把最终回答打回几轮，默认 2，0 表示只通知不拦截

[[hooks.pre_tool]]
match = "write_file|patch_file"   # 工具名，用 | 分隔，支持 * 通配；省略表示所有工具
command = "python3 .bunnybyte/hooks/guard.py"
timeout = 10                      # 秒，1–300，默认 10
on_error = "allow"                # 钩子自己出错/超时时：allow（放行并警告，默认）或 block

[[hooks.post_tool]]
match = "write_file|patch_file|run_shell"
command = "ruff check --quiet . || exit 2"

[[hooks.on_final]]
command = "python3 .bunnybyte/hooks/before_done.py"
only_if_changed = true            # 这一轮没改过文件就不跑，纯问答不触发
```

## 协议

钩子从 **stdin** 读到一份 JSON。字段名参考 Claude Code 的钩子，但**并不完全兼容**（见表后的差异），现成脚本通常要改几处：

| 字段 | 事件 | 含义 |
| --- | --- | --- |
| `hook_event_name`、`session_id`、`run_id`、`cwd` | 全部 | 事件名、会话、本次运行、仓库根目录 |
| `agent_depth`、`write_scope` | 全部 | 0 是主 agent，1 起是子 agent；子 agent 的写入范围（主 agent 为空） |
| `tool_name`、`tool_input` | pre_tool / post_tool | 工具名和参数（`write_file` 的 `content` 是完整内容） |
| `tool_response` | post_tool | 工具返回的文本（最多 20000 字） |
| `final_message`、`changed_paths`、`verification`、`stop_reason` | on_final | 最终回答、本轮改过的文件、验证回执状态、停止原因 |

和 Claude Code 的差异：工具名是 Bunny Byte 自己的（`write_file`、`patch_file`、`run_shell`、`read_file`……，不是 `Write` / `Edit` / `Bash`）；路径字段叫 `tool_input.path`，不是 `file_path`；`tool_response` 是字符串，不是对象；退出码 2 的反馈是 stdout 和 stderr 合并后的文本，不只是 stderr；不支持从 stdout 输出 JSON `decision`，也没有 `stop_hook_active`。

环境变量：`BUNNYBYTE_HOOK_EVENT`、`BUNNYBYTE_TOOL_NAME`、`BUNNYBYTE_PROJECT_DIR`。钩子的环境和 `run_shell` 一样经过过滤，API key 不会漏给它。工作目录是仓库根目录。

**退出码**决定结果，stdout/stderr（合并）的内容就是给模型看的理由：

| 退出码 | pre_tool | post_tool | on_final |
| --- | --- | --- | --- |
| 0 | 放行 | 无事发生 | 放行 |
| 2 | **拦截**这次工具调用，输出作为错误返回给模型；不会弹审批 | 输出作为反馈**追加到工具结果**里给模型看 | **扣住**最终回答，输出反馈给模型让它继续，最多 `max_final_rounds` 轮 |
| 其他、超时 | 钩子自己出错：默认放行，工具结果末尾附一行 `[hook warning]`；`on_error = "block"` 则按拦截处理 | 同左 | 同左，出错默认放行 |

细节：

- `pre_tool` 只能收紧，**不能替模型绕过审批**：钩子放行之后仍然要过审批策略和工具策略。
- `post_tool` 只跟随会写东西的工具（`write_file`、`patch_file`、`run_shell` 等）。它在工作区快照之前运行，所以格式化类钩子顺手改了的文件，照样算进这一轮的改动：`/undo` 能撤销、验证闸门会看到。
- `on_final` 在验证闸门之后运行。被打回的轮次不占模型调用预算，但模型为了修复而调用的工具仍然占 `max_steps`。轮次用完钩子仍然拦截时，最终回答照常交付，并带一行 ⚠ 提示。步数预算耗尽的收尾也会运行 `on_final`（适合做完成通知），但这时不能再打回。空转被拦（`loop_stalled`）和模型反复输出格式错误而放弃这两种收尾同样会运行（不能再打回）；用户中断、模型出错这类提前结束不会运行。在 `bunny -p` 里，如果反馈轮次用完后 `on_final` 仍然拦截，退出码是 5，JSON 结果里 `hooks.final_blocked` 为 true；交互模式下只会看到一行 ⚠ 提示。
- 钩子里用 `cmd &` 起的后台进程不会拖住钩子；钩子**超时**或被中断时，整个进程组（含这些后台进程）会被杀掉。
- 同一事件的多个钩子按顺序执行：`pre_tool` 和 `on_final` 遇到第一个拦截就停，`post_tool` 全部执行。父 agent 和后台 worker 共用同一组钩子，钩子一次只跑一个。但钩子改文件的时候，别的 worker 或主 agent 可能正好在两次快照之间，这种交叠下改动会被记到错误的一轮里（`/undo` 的范围可能不准），也不受 worker 的 `write_scope` 约束（`write_scope` 会放进 payload，钩子可以自己遵守）。需要这类钩子时不要同时用后台 worker。
- 子 agent（`agent` 工具启动的 worker）同样受 `pre_tool` / `post_tool` 约束；`on_final` 只在主 agent 上运行。
- 每次运行都会写进 `trace.jsonl` 的 `hook_run` 事件和 `report.json` 的 `hooks.runs`（输出脱敏、最多保留 40 条）。

## 信任：仓库里的钩子默认不运行

钩子是会在你的机器上执行的命令。你自己的全局配置和 `--config` 指定的文件直接生效，但**仓库里的 `.bunnybyte.toml` 可能来自别人**，里面的钩子在你确认之前一律不运行：

- 启动时会提示 “这个仓库的 .bunnybyte.toml 里有 N 条钩子，确认前不会运行”。
- `/hooks` 列出所有钩子和状态；确认内容没问题后运行 `/hooks trust`，指纹记在 `~/.bunnybyte/trusted_hooks.json`。之后钩子的**配置**只要有任何改动（命令、匹配、超时……），信任就失效，需要重新确认。
- 无人值守（`bunny -p`）或 CI：在启动 bunny 之前设置环境变量 `BUNNYBYTE_TRUST_HOOKS=1`，或把钩子写进全局配置。仓库自己的 `.env` 里设这个变量没有用（它只认进程启动时已有的环境）。
- 项目里写的 `max_final_rounds` 也只在项目钩子被信任之后才生效，并计入指纹。`.bunnybyte.toml` 是符号链接时，里面的钩子一律不运行。
- `bunny doctor` 有一行“钩子”显示运行中的数量、未信任的数量和配置错误。

已知的边界，请自己心里有数：

- 信任针对的是 `.bunnybyte.toml` 里的钩子**配置**，不包括它调用的脚本内容：`command = "./scripts/check.sh"` 被 `git pull` 改掉之后，指纹不变。要锁定脚本，请把命令写成带内容校验的形式，或放进全局配置。
- 钩子**不经过沙箱**直接运行（它们是你写的、在你确认之后才启用的）。
- `pre_tool` 是一道软防线，不是沙箱：配方 1 只匹配 `write_file` / `patch_file`，模型仍然可以用 `run_shell`（比如 `sed -i`）改同一个文件，甚至改掉守卫脚本本身。要真正限制，请把 `run_shell` 也纳入 `match` 并检查命令内容，或者用文件系统权限 / [沙箱](sandbox.md) 的 `deny_write`。
- 钩子出错默认放行（fail-open）。真正的安全闸门请设 `on_error = "block"`。

## 配方

**1. 禁止模型碰某些文件**（`.bunnybyte/hooks/guard.py`）：

```python
import json, sys

payload = json.load(sys.stdin)
path = str(payload["tool_input"].get("path", ""))
if path.endswith(".env") or path.startswith("migrations/"):
    print(f"{path} 受保护，不要修改；需要改动请在最终回答里说明。")
    sys.exit(2)
```

```toml
[[hooks.pre_tool]]
match = "write_file|patch_file"
command = "python3 .bunnybyte/hooks/guard.py"
on_error = "block"
```

**2. 每次写完文件自动格式化，并把 lint 报错塞回给模型**：

```toml
[[hooks.post_tool]]
match = "write_file|patch_file"
command = "ruff format --quiet . && ruff check --quiet . || exit 2"
```

格式化改动的文件会计入本轮改动（可以 `/undo`）；`ruff check` 报错时退出码 2，报错文本出现在这次工具结果的末尾，模型下一步就能看到并修掉。

**3. 交卷前的自定义条件**，例如不许留调试输出：

```toml
[[hooks.on_final]]
only_if_changed = true
command = "! git diff -U0 | grep -nE '^\\+.*(print\\(|console\\.log)' || { echo '改动里还有调试输出，请清理后再交卷'; exit 2; }"
```

运行测试请优先用内置的[验证回执](../README.md#验证回执)（`[verify]`）：它会留下带退出码和输出的回执，并检查回答里“测试通过”的说法。`on_final` 适合验证回执覆盖不到的、你自己定义的完成条件。

**4. 完成通知**（后台进程不会拖住钩子）：

```toml
[[hooks.on_final]]
command = "osascript -e 'display notification \"Bunny Byte 做完了\"' &"
# Linux：command = "notify-send 'Bunny Byte 做完了' &"
```
