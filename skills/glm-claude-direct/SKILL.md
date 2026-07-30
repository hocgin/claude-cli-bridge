---
name: glm-claude-direct
description: Delegate work to the local Claude Code CLI (GLM-backed) directly via Bash, with no MCP server in the path. Use when the glm_claude MCP worker is unavailable, the user explicitly wants to skip the bridge, or a one-shot sub-agent call is simpler than registering an MCP server — 直接调 claude, 不用 mcp, 绕过 bridge, 手动委派 claude, claude cli 直接跑, worker 挂了, 没装 mcp. This is the lightweight direct path; prefer the specialized MCP skills (glm-claude-task/research/review) when the bridge is connected, since they add identity prompts, async state, and re-review binding that this skill does not have.
---

# GLM/Claude direct CLI delegation

Spawn the Claude Code CLI (`claude -p`) as a subprocess to run a GLM-backed
sub-agent for one task. No MCP server, no daemon — just one command that blocks
until the sub-agent finishes and prints its answer as JSON.

```
you ──(Bash)──▶ claude -p "..." --output-format json ──▶ parse JSON ──▶ answer
```

## When to use this vs the MCP skills

| Situation | Use |
|---|---|
| `glm_claude` bridge connected, you want identity prompts / async / re-review | the MCP skills (`glm-claude-task`, `glm-claude-review`, …) |
| Bridge not installed, `ask_status` missing, or user said "don't use the MCP" | **this skill** |
| Quick one-shot where registering/configuring an MCP server is overkill | **this skill** |

This skill has **no state**: it cannot do the MCP bridge's identity prompts,
review-target binding, SHA-256 change detection, or task-list tracking. If you
need those, the bridge is the right tool. Do not fake them here.

## Prerequisite

Confirm the CLI exists once before dispatching:

```bash
claude --version
```

If it prints a version, proceed. If `claude` is not found, tell the user to
install the Claude Code CLI and stop — do not fall back to your own built-in
tools as a silent substitute.

## Dispatch a synchronous task

Run one task that blocks until the sub-agent returns. Capture stdout and parse
the JSON. The sub-agent runs with full tools (Bash/Read/Write/Edit/web) under
bypassed permissions, so describe the *what* and *where* and let it work:

```bash
claude -p "$(cat <<'EOF'
Review src/auth.py for SQL injection. Report concrete findings only.
EOF
)" \
  --output-format json \
  --model glm-5.2 \
  --effort low \
  --dangerously-skip-permissions \
  --max-budget-usd 2
```

Read the result. The successful response is a single JSON object; the fields
that matter:

- `is_error` — `false` on success. Check this first.
- `result` — the sub-agent's final text answer. This is your answer.
- `session_id` — a UUID. **Save it** if you may want to continue this
  conversation (see "Continue a session" below).
- `total_cost_usd` — what this call cost.
- `subtype` — `"success"`, or an error code like `"error_max_budget_usd"`.

A compact way to extract just the answer text and error flag:

```bash
claude -p "..." --output-format json --model glm-5.2 --effort low \
  --dangerously-skip-permissions --max-budget-usd 2 \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('ERROR' if d.get('is_error') else 'OK'); print(d.get('result',''))"
```

If `is_error` is true, report the `subtype` and the message in `errors[]` (or
`result`) instead of trusting the text as a real answer.

## Knobs

| Flag | What it does | When to change |
|---|---|---|
| `--model glm-5.2` | Default model (cheap/fast GLM). Use `sonnet` or `opus` for harder reasoning. | Hard reasoning, longer analysis |
| `--effort low` | Reasoning depth: `low`/`medium`/`high`/`xhigh`/`max`. | Raise for difficult problems |
| `--max-budget-usd 2` | Per-task spend cap. | Lower for a safety net; raise for big tasks |
| `--cwd` / run from a directory | Sets the sub-agent's working directory. | Always point at the relevant project root |

Set the working directory by running the command from there — the sub-agent's
file operations resolve against the shell's `cwd`.

## Continue a session

Each call returns a fresh `session_id`. To continue the same conversation (so
the sub-agent remembers prior context), reuse that id with `--resume`:

```bash
# First call — note the returned session_id
claude -p "Explain the architecture of src/" --output-format json \
  --model glm-5.2 --dangerously-skip-permissions --max-budget-usd 2

# Follow-up — resume the same conversation
claude -p "Now propose how to add a plugin system to it." --output-format json \
  --resume <session_id_from_above> \
  --model glm-5.2 --dangerously-skip-permissions --max-budget-usd 2
```

You are responsible for remembering the `session_id` between calls — there is no
registry like the bridge's. If the user closes the turn, the id is lost unless
you wrote it down. Pass it explicitly; never invent one.

## Long tasks: run in the background

`claude -p` blocks. For tasks that may take minutes, run it as a background
shell and write JSON to a file, then poll the file:

```bash
claude -p "Refactor the test suite and summarize changes." \
  --output-format json --model glm-5.2 --effort medium \
  --dangerously-skip-permissions --max-budget-usd 5 \
  > /tmp/claude-task-$(date +%s).json
```

Use the Bash tool's background mode (`run_in_background: true`) and read the
output file once the process exits. This is the direct equivalent of the
bridge's `ask_start` / `ask_wait`, but **without** cross-task listing or cancel
tracking — killing the background shell is your only cancellation, and you
cannot enumerate other running tasks.

## Result boundary

- Treat `result` as the sub-agent's work. If it edited files, verify the changes
  yourself (read the files, run tests) — it ran under bypassed permissions, so
  edits landed directly.
- Do not dispatch a second sub-agent to "verify" the first unless the user asks.
- If the answer is wrong or incomplete, either re-run with a sharper prompt or
  do the remaining work yourself — say which path you took.

## Cost awareness

GLM calls have a non-trivial fixed cost per invocation (a minimal "reply with
one word" call costs ~$0.14 in practice). Keep `--max-budget-usd` set as a
guardrail, batch related questions into one prompt instead of many, and prefer
`--resume` for follow-ups over starting fresh conversations.
