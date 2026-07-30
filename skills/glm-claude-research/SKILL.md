---
name: glm-claude-research
description: Delegate internet research to the glm_claude MCP worker. Use whenever the user needs web search, current facts, current-event lookup, fact checking, source gathering, library/version research, API docs lookup, market or pricing data, or anything that needs fresh online information — 联网搜索, 查资料, 查最新, 事实查证, 来源整理, 时效信息. Routes the work to the online-search identity, which runs a GLM-backed agent with web tools.
---

# GLM/Claude research delegation

Hand internet-research tasks to the `glm_claude` MCP worker's `online-search`
identity instead of searching yourself. The worker runs an isolated GLM-backed
sub-agent with web search/fetch tools and returns verified sources.

## Prerequisites

This skill assumes the `glm_claude` MCP server is registered. If the
`ask_status` / `ask_start` / `ask_wait` tools are not available, tell the user
the worker is missing and stop — do not fall back to your own web search without
saying so.

## How to delegate

1. **Check availability** once per task:

   ```text
   ask_status()
   ```

   Proceed only when it returns `ok: true` and `connected: true`. If discovery
   fails, report the error verbatim and stop.

2. **Dispatch the search** with the `online-search` identity. Pass only the
   research question — the worker injects the identity's behavioural rules:

   ```text
   ask_start(
     identity="online-search",
     prompt=<the complete research question, in the user's language>,
     cwd=<current project directory>,
     timeout_seconds=300
   )
   ```

   The worker's prompt need not repeat identity instructions. The default model
   (glm-5.2, low effort) suits most searches; override `model`/`effort` only for
   unusually deep research.

3. **Wait for completion**, polling at most 55s per call until the task reaches
   a terminal state or the dispatch timeout elapses:

   ```text
   ask_wait(task_id=<from step 2>, timeout_seconds=55)
   ```

   If the task is still non-terminal when the dispatch timeout is exhausted,
   call `ask_cancel` and report the timeout rather than re-dispatching.

4. **Use the returned `answer` as the sole research input.** The online-search
   identity is instructed to cite first-party/authoritative sources and to
   distinguish proven facts from claims and inferences. Surface that structure
   in your final answer.

## Result boundary

- Do **not** run your own web search, fetch the cited URLs, or dispatch a second
  worker to verify the result. That defeats the point of delegation and burns
  budget twice.
- Organize, translate, summarize, and format the returned material — that is
  your job as orchestrator.
- If the result is incomplete, contradictory, or lacks evidence, say so plainly
  in the final answer. Only re-search when the user explicitly asks.
