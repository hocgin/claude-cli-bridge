---
name: agent-routing
description: Route delegated work to four named GLM/Claude worker identities via the glm_claude MCP server. Use whenever a task requires internet search, current-information lookup, fact checking, source collection, code review, review follow-up, 联网搜索, 资料核对, 来源整理, 事实查证, 时效信息查询, 代码审核, 代码审查, 复审, or 再次审查. Send research to online-search and code reviews to S1, S2, and S3.
---

# GLM/Claude Agent Routing

The `glm_claude` MCP server wraps the local Claude Code CLI (which runs the GLM
model backend by default) as a sub-agent. The bridge owns the four identity
prompts and injects them via the CLI system prompt, so keep the identity scopes
distinct and never repeat identity instructions inside `prompt`.

## Runtime selection

1. Call `ask_status` before dispatching any identity.
2. Treat the worker as available only when the tool returns both `ok: true` and `connected: true`.
3. If the `glm_claude` MCP tools are missing or discovery fails, report that the worker is unavailable and stop. Do not silently switch to another route.

## Worker route

For every task, pass only the task body and its identity key. Do not prepend, quote, or repeat the identity instructions:

```text
ask_start(
  identity=<"online-search" | "S1" | "S2" | "S3">,
  prompt=<complete task body only>,
  cwd=<working directory>,
  timeout_seconds=300
)
```

Per-identity defaults (model / effort / allowed tools) are applied by the bridge unless you override `model` or `effort` explicitly:

- `online-search` — glm-5.2, low effort, full tool set (web search/fetch).
- `S1` — glm-5.2, low effort, Read/Grep/Glob only.
- `S2` — glm-5.2, medium effort, Read/Grep/Glob/Bash (to run dep scanners).
- `S3` — sonnet, high effort, Read/Grep/Glob only (deepest reasoning).

Then call `ask_wait` in intervals of at most 55 seconds until the task reaches a terminal state or the timeout has elapsed since dispatch. If the task is still non-terminal at that point, call `ask_cancel` and report the timeout; do not launch another execution route.

- The bridge runs every identity with `--dangerously-skip-permissions`; tool calls execute without interactive approval. Enforce each identity's behavioural restrictions through its identity prompt (e.g. "only textual review, do not edit files").
- Start S1, S2, and S3 before waiting for results. Each runs in its own subprocess with an isolated session id, so they execute concurrently.
- Return the worker findings to the main task; the orchestrator remains responsible for synthesis and final decisions.

## First code review

For a first S1/S2/S3 review, pass `review_target=<absolute reviewed file or directory>`:

```text
ask_start(
  identity=<"S1" | "S2" | "S3">,
  prompt=<review requirements only>,
  cwd=<same absolute project working directory>,
  review_target=<absolute reviewed file or directory>,
  timeout_seconds=300
)
```

This binds the returned session to that identity, working directory, and target, and records the file's SHA-256 so a later re-review can detect changes.

## Review continuation

When the user asks to review the same target again, treat it as a re-review unless they explicitly request a fresh or independent review:

```text
ask_start(
  identity=<"S1" | "S2" | "S3">,
  prompt=<current re-review requirements only>,
  cwd=<same absolute project working directory>,
  review_target=<same absolute reviewed file or directory>,
  resume_review=true,
  timeout_seconds=300
)
```

- Start S1, S2, and S3 re-reviews before waiting, as with first reviews. Each identity resumes its own most recently bound session for that target.
- The bridge loads the old conversation via `--resume` and injects the mandatory re-review protocol (regression check of all prior findings + complete incremental scan). Do not repeat identity instructions or manually paste the previous findings.
- If the bridge reports that no matching old session exists, or that identity/cwd/target validation failed, surface the failure. Never silently retry without `resume_review`, because that would create a new conversation.
- When the user explicitly asks for a fresh, independent, or clean-slate review, omit `resume_review` and start a new session with `review_target`.

## Result boundary

- After the worker returns a successful terminal result, treat that result as the sole input for the main task.
- Do not call web search, fetch source URLs, use another research connector, or dispatch another worker to verify, supplement, or repeat the result.
- Only organize, translate, summarize, compare, and format the returned material. Do not add unsupported current facts from the orchestrator's own knowledge.
- If the returned material is incomplete, contradictory, or lacks evidence, state that limitation in the final answer instead of searching again.
- Perform another search only when the user explicitly asks for a new search or re-verification.

## Routing rules

- Internet research, current facts, fact checking, or source gathering: use `online-search`.
- Code review: use S1, S2, and S3 together unless the user explicitly requests one identity.
- A mixed research and code-review request may use `online-search` plus all three reviewers.
- Explicit identity names from the user override automatic routing.
