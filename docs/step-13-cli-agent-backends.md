# Step 13 - CLI 编码代理后端（Claude Code / Codex / Pi）

## 实现状态注记（2026-07-11）

1. `app/services/cli_agent_service.py` 已实现三个 runner：`claude_code`、`codex`、`pi`，由 `AgentService` 按 `run_mode` 分发。
2. Pi runner 基于 `@mariozechner/pi-coding-agent` 的 `pi -p --mode json` 一次性 JSONL 事件流接入，支持多轮会话续接。
3. 测试：`tests/test_cli_agent.py` 42 项（三 runner 的工厂注册 / 命令构造 / 事件解析、假 pi 子进程集成含崩溃 / 超时降级 / exit0-无结果 / 环境变量白名单——子进程骨架三 runner 共享故只测一次、API 级 SSE 全链路含会话续接），全量套件 469 passed（1 项环境依赖跳过）。

## 1. 文档目标

说明「把外部编码代理 CLI 内嵌为 CaiBao 运行后端」这条链路的设计与实现：为什么选 run_mode 分发而不是工具注册表、三个 runner 如何共享一套子进程骨架、以及 Pi 后端的接入细节，供后续新增 runner（如 Gemini CLI / Qwen Code）时照抄。

## 2. 先给结论

1. CLI 编码代理不是一个"工具"，而是一种**运行策略**：`AgentRunRequest.run_mode ∈ CLI_AGENT_MODES` 时，整个 run 委派给外部 CLI，内置 FC 循环不参与。
2. 所有 runner 复用 `BaseCliAgentRunner` 的执行骨架：`subprocess.Popen`（行缓冲、`start_new_session`）+ stderr 后台收集 + watchdog 超时 + 进程组终止，子类只实现 `build_command()` 和 `parse_line()` 两个纯函数。
3. CLI 的 JSONL 输出统一归一化为 `CliAgentEvent`（session / text / tool_use / tool_result / warning / result / error 七种），由 `AgentService._run_cli_agent_stream` 映射为既有 `AgentStreamEvent` SSE 协议与 `AgentStep` 持久化——前端与回放零改动。
4. Prompt 一律走 stdin 传入，不进 argv，避免注入与超长命令行问题。

## 3. 调用链

1. 前端 App 工作台「运行模式」选择 `pi`（`app/web/index.html` 的 `#agentAppModeSelect`），或 API 调用 `POST /api/v1/agent/runs` 携带 `"run_mode": "pi"`。
2. 模式校验只发生在 App 保存路径：`AgentAppService._ALLOWED_MODES` 在创建/更新 App 时校验 `mode`；直接 `POST /api/v1/agent/runs` 的 `run_mode` 不做枚举校验——`CLI_AGENT_MODES` 之外且非 `workflow` 的值会静默回落到内置 `agent_auto` 循环（与既有 run_mode 语义一致）。
3. `AgentService.stream_run()` → `_run_cli_agent_stream()`：检查 `CLI_AGENT_ENABLED`，解析每会话工作区 `data/cli_agent_workspaces/<team>/<conversation>`，读取上次 CLI session id。
4. `create_cli_agent_runner(mode, settings)` 构造 runner，`runner.run()` 产出 `CliAgentEvent` 流。
5. 事件映射：`text` → SSE `llm.delta`；`tool_use`/`tool_result` → `tool.started`/`tool.result` + `AgentStep(tool_call)`；`result` → 最终答案 + `step.completed`；`error` → run 失败。
6. run 结束后 CLI session id 写回工作区 `.caibao_cli_sessions.json`，同会话下次运行自动续接。

## 4. Pi 后端接入细节

### 4.1 命令构造（`PiRunner.build_command`）

| 片段 | 条件 | 说明 |
|------|------|------|
| `pi --print --mode json` | 恒定 | 非交互一次性运行，事件以 JSONL 打到 stdout |
| `--no-extensions --no-skills --no-prompt-templates --no-themes` | 恒定 | 内嵌运行禁用发现机制：扩展可能弹出无法应答的 UI 对话框导致挂起 |
| `--session-dir <workdir>/.pi-sessions` | 恒定 | 会话文件落在会话工作区内，不污染宿主机 `~/.pi` |
| `--provider / --model / --thinking` | 配置非空 | 见 4.4 配置表 |
| `--no-tools` | `dry_run=true` | 降级为纯分析，不做任何文件/命令操作 |
| `--append-system-prompt <text>` | system_prompt 非空 | 与 Claude Code runner 同语义 |
| `--session <id>` | 有续接 id | id 来自上次运行的 session 头事件 |
| prompt | 恒定 | 经 stdin 写入（`pi -p` 支持从 stdin 读取 prompt） |

### 4.2 事件映射（`PiRunner.parse_line`）

| pi JSON 事件 | CliAgentEvent | 备注 |
|--------------|---------------|------|
| `session`（首行头） | `session` | 取 `id` 供续接 |
| `message_update` + `text_delta` | `text` | 增量流式；`thinking_delta` 忽略 |
| `tool_execution_start` | `tool_use` | `toolCallId` / `toolName` / `args` |
| `tool_execution_end` | `tool_result` | `result.content` 拍平为文本，`isError` 定成败 |
| `message_end`（stopReason=error/aborted） | —（记账） | 暂存失败原因；正常收尾则清除（自动重试成功场景） |
| `agent_end` | `result` | 成功时从 messages 提取最后一条 assistant 文本作为最终答案；messages 里最后一条 assistant 以 error/aborted 收尾则定性失败（覆盖 pi 只发 `agent_end` 的循环内异常路径） |
| `auto_retry_start` | `warning` | 记为 observation 步骤 |
| 其余（`turn_*`、`message_start` 等） | 忽略 | 文本增量已覆盖，避免重复 |

注：`extension_error` 仅存在于 `--mode rpc`；json 模式下 pi 把扩展错误只写到 stderr。解析分支作前向兼容保留。

### 4.3 失败定性

1. LLM 中断：`message_end.stopReason ∈ {error, aborted}` → `agent_end` 返回 `result(ok=False)`，携带 `errorMessage`；若 pi 自动重试成功（默认开启，最多 3 次），后续正常 `message_end` 会清除该标记，最终 `result(ok=True)` 携带真实答案。
2. 循环内异常（pi 的 handleRunFailure 路径）：只发 `agent_end` 不发 `message_end`，由 `agent_end.messages` 最后一条 assistant 消息的 stopReason 定性。
3. 进程启动前失败（如未配 API key）：pi 以非零码退出且无 result 事件，基类产出 `error` 事件带 stderr 尾部。**注意 json 模式下运行中的失败 pi 仍以 0 退出，`result.ok` 是唯一失败信号。**
4. 超时：watchdog 到 `CLI_AGENT_TIMEOUT_SECONDS` 后杀进程组，产出超时 `error`。

### 4.4 配置

| 环境变量 | 默认 | 说明 |
|----------|------|------|
| `CLI_AGENT_ENABLED` | `false` | 三后端总开关 |
| `CLI_AGENT_PI_PATH` | `pi` | pi 可执行文件路径（`npm i -g @mariozechner/pi-coding-agent`） |
| `CLI_AGENT_PI_PROVIDER` | 空 | 空则用 pi 默认 provider；如 `anthropic` / `openai` / `deepseek` |
| `CLI_AGENT_PI_MODEL` | 空 | 模型 pattern，支持 `provider/id` 形式 |
| `CLI_AGENT_PI_THINKING` | 空 | `off` ~ `xhigh` |
| `CLI_AGENT_EXTRA_ENV_JSON` | 空 | 传给 CLI 子进程的额外环境变量（放 `ANTHROPIC_API_KEY` 等） |

其余 `CLI_AGENT_WORKSPACE_ROOT` / `CLI_AGENT_TIMEOUT_SECONDS` / `CLI_AGENT_MAX_STEP_OUTPUT_CHARS` 三后端共用。

Claude Code / Codex 的对应变量：`CLI_AGENT_CLAUDE_PATH` / `CLI_AGENT_CLAUDE_MODEL`（空则用 CLI 默认）/ `CLI_AGENT_CLAUDE_PERMISSION_MODE`（默认 `acceptEdits`，`dry_run` 强制 `plan`）/ `CLI_AGENT_CLAUDE_ALLOWED_TOOLS`（逗号分隔，传 `--allowedTools`）；`CLI_AGENT_CODEX_PATH` / `CLI_AGENT_CODEX_MODEL` / `CLI_AGENT_CODEX_SANDBOX`（默认 `workspace-write`，`dry_run` 强制 `read-only`）。全部见 `.env.example`。

## 5. 测试方法

1. 假 pi 脚本（`tests/test_cli_agent.py` 的 `_FAKE_PI_SCRIPT`）：Python 写的 JSONL 回放器 + `#!/bin/sh` wrapper 伪装成 `pi` 可执行文件，与 `test_mcp_client.py` 的假 MCP server 同一套路，无需安装真实 CLI、不调用 LLM。
2. 覆盖：模式注册与工厂、命令构造（默认/全参/续接/dry_run）、逐事件解析（含垃圾行容错）、子进程集成（正常流 + 崩溃流 stderr 透传）、API 级 SSE 全链路（`run_mode=pi` 从创建 run 到 `run.completed`，及未启用时的 `run.failed`）。

## 6. 安全边界（务必阅读）

CLI 编码代理是**能自主执行 shell / 文件操作的代理**，把 run 委派给它等于把宿主机的一段执行能力交出去。默认关闭（`CLI_AGENT_ENABLED=false`），启用前须明确以下边界：

1. **环境隔离（已实现）**：子进程不再整份继承服务器环境，只透传 `_ENV_ALLOWLIST` 白名单（`PATH` / `HOME` / locale / 代理 / CA），杜绝 `AUTH_JWT_SECRET`、`DATABASE_URL`、provider key 被 CLI 内 shell（`env`）读出。CLI 自身凭据走 `~/.claude`、`~/.codex/auth.json` 等（HOME 可达），或经 `CLI_AGENT_EXTRA_ENV_JSON` 显式注入——服务器进程环境里的 key 不再自动透传。
2. **工作区隔离（已实现，leaf 级）**：每会话独立目录 `<root>/<team>/<conversation>`，`_safe_path_component` 单射清洗（对丢失信息的 id 追加摘要）防跨租户目录碰撞；prompt 走 stdin 不进 argv。
3. **文件系统读隔离（⚠️ 未实现，已知限制）**：`workdir` 只设进程 cwd，**不是 chroot/读监狱**。codex `workspace-write` 只限写不限读，claude `acceptEdits` 无沙箱——一旦 CLI 拿到 shell，可读取宿主机上进程可达的任意文件（含应用 SQLite DB、其他租户工作区），OS 层绕过应用级 `team_id` 边界。**因此：(a) 不要把 CaiBao 的 DB / `.env` 与 `cli_agent_workspace_root` 放在 CLI 进程可达的同一路径树；(b) 仅对可信团队成员开放本功能；(c) 生产强隔离需为每次 run 套 OS 沙箱（Linux `unshare`+bind-mount / `landlock`、macOS `sandbox-exec` / 容器），见后续。**

## 7. 局限与后续

1. CLI 必须安装在服务端宿主机；Docker 镜像未打包 Node.js，容器内启用需自行扩展镜像（与 MCP 的容器说明同口径）。
2. 一次 run 对应一次 CLI 进程；pi 的 RPC 常驻模式（`--mode rpc`，支持 steer / follow_up / abort 中途干预）留作后续增强，接入点仍是本文的 runner 抽象。
3. `dry_run` 对 pi 采用 `--no-tools` 近似（无沙箱只读模式），比 codex 的 `read-only` sandbox 语义更强，注意预期差异。
4. **OS 级 run 沙箱**：为文件系统读隔离补齐每次 run 的命名空间 / seccomp / landlock 或容器隔离，使 CLI 进程只能看到本会话工作区——这是把本功能从"可信内网工具"提升到"多租户安全"的关键后续。
