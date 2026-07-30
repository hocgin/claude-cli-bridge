# Skills

场景化 skills，覆盖两种委派 GLM/Claude 子智能体的路径：**MCP 桥**（有状态、多身份）
和**直连 CLI**（无状态、轻量）。按用户意图和可用性触发，而不是把所有任务塞进一个通用路由。

## MCP 桥路径（需 `glm_claude` server）

| Skill | 触发场景 | 路由到的身份 / 工具 |
|---|---|---|
| `glm-claude-task` | 任意任务委派（编码 / 文件 / 命令 / 混合） | `ask_task`（全工具，同步） |
| `glm-claude-research` | 联网检索、查最新、事实查证、来源整理 | `online-search` |
| `glm-claude-review` | 代码审查（语法 / 依赖安全 / 规范可维护性） | `S1` + `S2` + `S3` 并发 |
| `glm-claude-rereview` | 复审同一目标的修改（回归 + 增量） | `S1`/`S2`/`S3` 各自 `resume_review` |

## 直连 CLI 路径（无需 MCP server）

| Skill | 触发场景 | 实现方式 |
|---|---|---|
| `glm-claude-direct` | bridge 未安装 / `ask_status` 不可用 / 用户明确要求绕过 MCP / 想要轻量一次性调用 | 直接 `claude -p` 子进程 + Bash |

## 两条路径怎么选

- **bridge 在线、需要身份提示/异步任务/复审绑定** → 用 MCP 桥 skills。
- **bridge 不可用，或只要一次性派任务拿结果** → 用 `glm-claude-direct`。
- 直连路径**无状态**：没有身份提示、审查目标绑定、SHA-256 变更检测、任务列表。
  需要这些时回归 MCP 桥，不要在直连路径里伪造。

## 前置依赖

这些 skills 只负责**编排**，实际能力由 `glm_claude` MCP server 提供。使用前必须：

1. 安装 Claude Code CLI（`claude --version` 可用）。
2. 安装并注册 `glm_claude` bridge：
   ```bash
   cd codex-glm-desktop-bridge
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. 把 `.codex/mcp.example.json` 的 `glm_claude` server 配置并入你的 MCP 配置。

`ask_status` 返回 `connected: true` 才代表 worker 可用；不可用时 skills 会显式报错，
不会静默降级到助手的内置搜索/审查。

## 安装 skills

把需要的目录复制到 ZCode/Codex 的 skills 发现路径之一（用户级推荐 `~/.agents/skills/`）：

```bash
# MCP 桥 skills（需先装好 glm_claude bridge）
cp -r skills/glm-claude-task     ~/.agents/skills/
cp -r skills/glm-claude-research ~/.agents/skills/
cp -r skills/glm-claude-review   ~/.agents/skills/
cp -r skills/glm-claude-rereview ~/.agents/skills/

# 直连 CLI skill（只需本机有 claude CLI，无需 MCP server）
cp -r skills/glm-claude-direct ~/.agents/skills/

# 或只装你需要的，例如只装通用任务委派
cp -r skills/glm-claude-task ~/.agents/skills/
```

复制后重启客户端即可。触发是自动的——描述里包含了中英双语触发词（"查资料"
触发 research，"审查代码"触发 review，"复查之前的问题"触发 rereview）。

## 各 skill 的边界

- **research** 一次联网检索，单身份。
- **review** 首次审查，三身份并发，绑定 `review_target` + 记录 SHA-256 基线。
- **rereview** 只能用于**已审查过**的目标；它会续接上轮会话，对比哈希，执行
  回归检查 + 增量扫描。目标从未审查过时，用 `review` 而非 `rereview`。
- **direct** 无 MCP server 时的回退：直接 `claude -p`，无身份、无状态、无复审。
  bridge 在线时优先用上面三个，而非 direct。
