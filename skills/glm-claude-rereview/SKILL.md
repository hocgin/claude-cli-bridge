---
name: glm-claude-rereview
description: Delegate a re-review of a previously reviewed target to the glm_claude MCP worker. Use whenever the user wants to re-check, re-audit, follow up on, or see if earlier review findings are fixed — 复审, 再次审查, 重新审查, 复查, 看看之前的问题修了没, 回归检查. Resumes each reviewer's prior session, compares the file's current SHA-256 against the baseline, and enforces a regression-check + incremental-scan protocol.
---

# GLM/Claude re-review delegation

A *re-review* is not a fresh review — it continues the prior review session so
each reviewer can check whether its earlier findings were fixed and scan for new
issues. Use this skill when the target was reviewed before (by S1/S2/S3) and the
user wants a follow-up. For a brand-new review, use `glm-claude-review` instead.

## Prerequisites

Requires the `glm_claude` MCP server **and** a prior bound review session for
the same target. If no prior session exists, the bridge will refuse — surface
that error and offer a fresh review (`glm-claude-review`) rather than silently
starting one.

## When to treat a request as a re-review

Treat it as a re-review when **all** are true:

- The same file/directory was reviewed by S1/S2/S3 before in this session or
  project.
- The user wants a follow-up: "再看一遍", "复查", "之前的问题修了吗", "re-check".

Treat it as a **fresh** review when the user explicitly says so ("从头审查",
"independent review", "clean slate", first time) or the target was never
reviewed — fall back to `glm-claude-review`.

## How to run a re-review

1. **Check availability** once:

   ```text
   ask_status()
   ```

2. **Dispatch S1, S2, S3 with `resume_review=true`**, all before waiting. Pass
   the **same** `cwd` and `review_target` used in the original review:

   ```text
   ask_start(
     identity="S1",
     prompt=<this round's extra requirements only>,
     cwd=<same absolute project working directory>,
     review_target=<same absolute reviewed file or directory>,
     resume_review=true,
     timeout_seconds=300
   )
   ask_start(identity="S2", ...)
   ask_start(identity="S3", ...)
   ```

   What the bridge does automatically — do **not** do these yourself:
   - Finds each identity's most-recently-bound session for that target.
   - Resumes it with `--resume` so the prior findings are in context.
   - Compares the file's current SHA-256 against the baseline recorded at first
     review, and tells the reviewer what changed.
   - Injects the two-part protocol (regression check + independent incremental
     scan), so you must **not** paste the prior findings into `prompt`.

3. **Wait for each** by `task_id`, polling up to 55s per call.

## What a re-review answer contains

Each reviewer's answer has two parts, in order:

1. **Regression check** — every prior finding marked
   已修复 / 部分修复 / 未修复 / 无法验证 (fixed / partial / not fixed / unverified),
   with current code evidence.
2. **Incremental scan** — newly introduced or previously missed issues, found
   independently of the old list, de-duplicated against the regression items.

## Synthesis & result boundary

- Consolidate the three reviewers' regression + incremental findings as in
  `glm-claude-review`, ranking fixes and new issues separately.
- If the bridge reports `resume_session_id` validation failed (identity / cwd /
  target mismatch, or no bound session), **stop and report it**. Do not retry
  without `resume_review` — that would silently create a brand-new conversation
  and lose the regression check, which is the whole point.
- Do not dispatch another worker to verify, and do not re-review files yourself.
