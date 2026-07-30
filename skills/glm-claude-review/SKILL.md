---
name: glm-claude-review
description: Delegate code review to the glm_claude MCP worker's three reviewers. Use whenever the user wants code reviewed, audited, checked for bugs, syntax, dependency/security risk, naming/style/maintainability — 代码审核, 代码审查, 审查代码, 检查语法, 依赖安全, 规范检查, 可维护性. Runs S1 (syntax), S2 (dependency/security), and S3 (standards/maintainability) as concurrent GLM-backed sub-agents and synthesizes their findings.
---

# GLM/Claude code review delegation

Hand code review to the `glm_claude` MCP worker rather than reviewing files
yourself. Three specialized reviewer identities run concurrently, each with a
constrained tool set, and you synthesize their findings into one verdict.

## Prerequisites

Requires the `glm_claude` MCP server. If `ask_status` / `ask_start` /
`ask_wait` are missing, tell the user the worker is unavailable and stop.

## The three reviewers

| Identity | Scope | Default model / effort | Tools |
|---|---|---|---|
| `S1` | syntax, typos, low-level code errors | glm-5.2, low | Read, Grep, Glob |
| `S2` | dependency vulnerabilities, outdated/risky deps, supply-chain | glm-5.2, medium | Read, Grep, Glob, Bash |
| `S3` | naming, style, structure, maintainability | sonnet, high | Read, Grep, Glob |

The bridge injects each identity's prompt and forbids them from editing files
or using a browser. Run all three unless the user asks for one specifically.

## How to run a review

1. **Check availability** once:

   ```text
   ask_status()
   ```

2. **Dispatch S1, S2, S3 before waiting** — they run in isolated subprocesses, so
   starting them all first maximizes concurrency. Pass the review target as an
   absolute path and the requirements as `prompt`:

   ```text
   ask_start(
     identity="S1",
     prompt=<review requirements only>,
     cwd=<absolute project working directory>,
     review_target=<absolute file or directory to review>,
     timeout_seconds=300
   )
   ask_start(identity="S2", ...)   # same cwd + review_target
   ask_start(identity="S3", ...)
   ```

   `review_target` binds each returned session to (identity, cwd, target) and
   records the file's SHA-256 — this is what later re-reviews build on.

3. **Wait for each** by its `task_id`, polling up to 55s per call:

   ```text
   ask_wait(task_id=<s1 task id>, timeout_seconds=55)
   ```

   If any reviewer is still running when the dispatch timeout elapses, cancel it
   with `ask_cancel` and note the partial result.

## Synthesis

Collect the three `answer` fields. Your job as orchestrator:

- **De-duplicate** findings the reviewers flagged in common.
- **Rank by severity**: S2 security findings usually rank above S1 syntax nits,
  but weigh per the user's concern.
- **Preserve evidence**: each finding should keep the code location the reviewer
  cited. Don't invent locations.
- Give one consolidated verdict. Do **not** dispatch another worker to verify,
  and don't re-review files yourself.

If a reviewer returned `state: failed`, report its `error` and synthesize from
the rest — don't silently drop the failure.
