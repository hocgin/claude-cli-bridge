# codex-glm-desktop-bridge

把本机的 **Claude Code CLI**（默认走 GLM 模型后端）封装成一个 **MCP Server**，
供 Codex 当作子智能体调用。参考 [Codex-WorkBuddy-Desktop-Bridge](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge)
的架构，但把它的 ACP 通信层整体替换为一次 `claude -p` 子进程调用——CLI 本身已经自带
agent 循环、工具执行、会话持久化与流式输出，因此参考仓库里最复杂的 `acp.py` /
`multiplexer.py` / `history.py` 三块在此塌缩成一个轻量 subprocess 封装。

```
Codex ──(MCP stdio)──▶ claude_cli_bridge(FastMCP) ──(subprocess)──▶ claude -p
                                                                (内部跑 GLM)
```

## 能力一览

| 能力 | 说明 |
|---|---|
| 6 个 MCP 工具 | `ask_status` / `ask_start` / `ask_wait` / `ask_cancel` / `ask_list` / `ask_task` |
| 任意任务委派 | `ask_task` 同步执行任意任务，全工具权限（Bash/Edit/Read/Write/联网），可追踪可取消 |
| 异步任务 | `start` 立即返回 `task_id`，后台线程跑，态机 `queued→running→completed/failed/cancelled` |
| 任务隔离 | 每任务独立 `claude -p` 子进程 + 独立 session-id + 可选独立 cwd，天然并发隔离 |
| 四身份系统 | `online-search`（联网检索）/ `S1`（语法）/ `S2`（依赖安全）/ `S3`（规范可维护性） |
| 异构模型分工 | 每身份默认 model/effort 不同：搜索/S1 用 glm-5.2 低成本，S3 用 sonnet 高推理 |
| 工具权限约束 | 审查身份只给 Read/Grep/Glob，搜索身份保留联网工具 |
| 首审绑定 | S1/S2/S3 带上 `review_target`，把会话绑定到（身份, 目录, 目标）并记录文件 SHA-256 |
| 复审 / 会话复用 | `resume_review=true` 自动找回上轮会话用 `--resume` 续接，注入回归+增量双协议 |
| 变更感知 | 复审时对比上轮基线 SHA-256，告知模型"哪些变了"，防止机械复述 |
| 防误用护栏 | 身份/目录/目标不匹配或找不到旧会话时拒绝续接，而非悄悄新建 |
| 成本预算 | `--max-budget-usd` 每任务设上限 |
| 流式日志 | 每任务落一份 JSONL 事件日志，完整可回放 |

## 安装

```bash
cd claude-cli-bridge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

前提：本机已安装 Claude Code CLI（`claude --version` 可用）。若你的 CLI 已配置走 GLM
网关，则无需任何额外鉴权；否则按 CLI 官方方式登录或设置 `ANTHROPIC_API_KEY`。

## 注册到 Codex

把 `.codex/mcp.example.json` 的内容并入你的 Codex MCP 配置（`~/.codex/config.toml`
的 `[mcp_servers]` 段，或项目 `.mcp.json`）。路由 skill 在 `.codex/skills/agent-routing/`，
Codex 会据此把联网任务路由给 `online-search`、代码审查路由给 S1/S2/S3。

## 工具速查

```python
ask_status(task_id="")                         # 探活 / 查单个任务
ask_task(prompt, cwd="", system_prompt="",     # 同步执行任意任务(全工具)
         model="", effort="", add_dirs=[],
         max_budget_usd=5.0, timeout_seconds=600)
ask_start(prompt, cwd="", identity="",         # 异步派发任务(身份导向)
          model="", effort="",                  #   覆盖身份默认
          review_target="",                     #   S1/S2/S3 首审绑定
          resume_session_id="",                 #   续接指定会话
          resume_review=False,                  #   自动找回+复审协议
          max_budget_usd=2.0, timeout_seconds=300)
ask_wait(task_id, timeout_seconds=55)          # 阻塞等待
ask_cancel(task_id)                            # 终止子进程
ask_list()                                     # 列出已知任务
```

`ask_task` 是通用委派入口：全工具权限、同步返回，适合任意自由任务。
`ask_start` 是身份导向入口：`prompt` 只传**任务正文**，身份指令由 bridge
经 `--append-system-prompt` 注入，不要重复。

配套 skills 在 `skills/` 下，共四个（task / research / review / rereview），
可分发安装。

## 测试

```bash
.venv/bin/python tests/smoke_mcp.py        # 基础链路:Codex→MCP→claude -p→返回
.venv/bin/python tests/smoke_task.py       # ask_task 任意任务委派(全工具+同步)
.venv/bin/python tests/smoke_identity.py   # 身份系统 + 首审绑定 + 复审会话复用
```

## 与参考仓库的对照

| 参考仓库模块 | 本项目 | 说明 |
|---|---|---|
| `acp.py`（ACP 发现/spawn/client） | `cli_worker.py` | JSON-RPC/SSE → 一次 `claude -p` subprocess |
| `multiplexer.py`（事件多路复用） | 删除 | 一任务一进程，流式即 stdout，无需路由 |
| `history.py`（写 WorkBuddy SQLite） | 删除 | CLI 自带会话持久化（`--session-id`/`--resume`） |
| `identities.py`（4 身份提示词） | `identities.py` | 原样保留 + 增加每身份默认 model/tools/effort |
| `review_sessions.py`（复审注册表） | `review_sessions.py` | 保留注册表 + SHA-256 diff，transcript 改用 `--resume` |
| `server.py`（FastMCP 工具） | `server.py` | 同结构、同工具名，内部接 CLI |
| `.codex/skills/SKILL.md` | `.codex/skills/agent-routing/SKILL.md` | 适配后的路由规则 |

## 权衡

CLI 方案相比直连 API 的唯一额外开销是每次调用约 0.5–1s 的进程冷启动。对于本项目的
任务粒度（一次审查、一次搜索，本身数秒到数分钟）完全可忽略；高频小调用才需回退直连 API。
