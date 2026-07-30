---
name: glm-claude-task
description: Delegate an arbitrary task to the glm_claude MCP worker and get the result back synchronously. Use whenever the user wants to hand off any free-form coding, file, shell, or research task to a GLM-backed sub-agent that can read/edit files and run commands on its own — 跑个任务, 委派任务, 让它去做, 帮我执行, 调用子智能体, 调个 GLM, 用 claude 做这件事, 交给子agent. This is the general-purpose entry point; prefer the specialized skills for search/review/re-review when they fit.
---

# GLM/Claude arbitrary task delegation

The `ask_task` tool runs a fresh isolated GLM-backed sub-agent (the Claude Code
CLI) with the **full default tool set** — Bash, Read, Write, Edit, web search —
under bypassed permissions, blocks until it finishes, and returns the final
answer in one call. Use it when the task doesn't fit the specialized identities
(online-search / S1 / S2 / S3) and you want the worker to act autonomously.

## Prerequisites

Requires the `glm_claude` MCP server. If `ask_task` / `ask_status` are missing,
tell the user the worker is unavailable and stop.

## When to use this vs the specialized skills

| Want | Use |
|---|---|
| Pure internet research / fact lookup | `glm-claude-research` (cheaper, scoped) |
| Code review (syntax/security/style) | `glm-claude-review` |
| Re-review of a previously reviewed target | `glm-claude-rereview` |
| **Anything else** — multi-step coding, file edits, running commands, mixed work, or you're unsure | **this skill / `ask_task`** |

When in doubt, `ask_task` is the safe general choice. The specialized skills
exist because they're cheaper and better-bounded, not because `ask_task` can't
do those jobs.

## How to delegate

1. **Check availability** once:

   ```text
   ask_status()
   ```

   Proceed only when it returns `ok: true` and `connected: true`.

2. **Dispatch the task** with `ask_task`. This call blocks until completion, so
   send the full goal and let the worker act autonomously:

   ```text
   ask_task(
     prompt=<the complete task: goal, constraints, relevant paths>,
     cwd=<project root the task concerns>,
     timeout_seconds=600
   )
   ```

   The worker has full tools, so it can read files, edit code, and run commands
   inside `cwd`. Describe the *what* and *where*; let it work out the *how*.

3. **Read the returned `answer`** — it is the worker's final result. If
   `state` is `failed`, report `error` instead. If `state` is `cancelled`, say
   so.

## Optional knobs

- **`system_prompt`** — fully replaces the default instructions when you need
  tight control over the worker's behaviour or role. Omit for plain tasks.
- **`model`** — `glm-5.2` (default, cheap/fast) for routine work; `sonnet` or
  `opus` for harder reasoning.
- **`effort`** — `low` / `medium` / `high` / `xhigh` / `max`. Raise it for hard
  problems.
- **`add_dirs`** — extra absolute directories the worker may touch beyond `cwd`
  (e.g. a shared config dir outside the project).
- **`max_budget_usd`** — per-task spend cap. Default is higher than the identity
  tools since arbitrary tasks may run longer; lower it to be safe.

## Cancelling & tracking

`ask_task` blocks synchronously, but the task stays queryable and cancellable
while it runs (and after):

```text
ask_status(task_id=<from ask_task>)   # inspect one task
ask_list()                            # see all tasks
ask_cancel(task_id=...)               # interrupt a long runner
```

If a task is clearly off-track, cancel it rather than waiting out the timeout.

## Result boundary

- Treat the worker's `answer` as the result of the delegated work. If it edited
  files, verify the changes yourself (read the files, run tests) — the worker
  runs under bypassed permissions, so its edits are applied directly.
- Do not dispatch a second worker to "verify" the first unless the user asks.
  Inspect the output yourself.
- If the answer is incomplete or wrong, either re-dispatch with a sharper
  prompt or do the remaining work directly — surface which path you took.
