"""FastMCP server exposing the Claude Code CLI as an MCP worker.

Structural sibling of workbuddy_bridge/server.py: same five tools
(status / start / wait / cancel / list), same thread-per-task state machine,
same async-dispatch-then-poll model.  The only difference is what runs under
each task -- here it is an isolated ``claude -p`` subprocess instead of a
WorkBuddy ACP session.

Lifecycle of one task::

    ask_start  ──▶  TaskState(queued)  ──▶  thread ──▶  claude -p
                      │                                     │
                      │                                     │ stream-json
                      ▼                                     ▼
                   ask_wait polls state            ActivityLogger.feed
                      │                                     │
                      ▼                                     ▼
                 completed/failed/cancelled        terminal result event
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .activity_log import ActivityLogger
from .cli_worker import (
    CliError,
    TaskHandle,
    TaskRequest,
    TaskResult,
    locate_cli,
    run_task,
)
from .identities import IdentityOverrides, REVIEW_IDENTITIES
from .review_sessions import (
    ReviewResume,
    bind_review_session,
    build_rereview_prompt,
    find_review_session,
    normalize_review_target,
    normalize_session_id,
    prepare_review_resume,
    target_sha256,
)

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / "work" / "glm-claude-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

mcp = FastMCP(
    "glm_claude_worker",
    instructions=(
        "Delegate bounded tasks to the local Claude Code CLI worker. "
        "The CLI runs the GLM model backend by default and owns the agent "
        "loop, tool execution and session persistence."
    ),
    log_level="WARNING",
)

_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})


@dataclass
class TaskState:
    task_id: str
    prompt: str
    cwd: str
    identity: str = ""
    model: str = ""
    effort: str = ""
    max_budget_usd: float = 2.0
    session_id: str = ""
    resume_session_id: str = ""
    # Plain-task flexibility (ask_task): a custom system prompt fully replaces
    # the identity prompt, and add_dirs extends the CLI's accessible paths.
    system_prompt: str = ""
    add_dirs: tuple[str, ...] = ()
    review_target: str = ""
    resume_review: bool = False
    resumed: bool = False
    previous_sha256: str | None = None
    current_sha256: str | None = None
    state: str = "queued"
    answer: str = ""
    cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    model_usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = ""
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested: bool = False
    handle: TaskHandle | None = None
    condition: threading.Condition = field(default_factory=threading.Condition)


TASKS: dict[str, TaskState] = {}
TASKS_LOCK = threading.Lock()


def _public(task: TaskState) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "state": task.state,
        "session_id": task.session_id,
        "cwd": task.cwd,
        "identity": task.identity or None,
        "model": task.model or None,
        "effort": task.effort or None,
        "max_budget_usd": task.max_budget_usd,
        "resumed": task.resumed,
        "resume_review": task.resume_review,
        "review_target": task.review_target or None,
        "previous_sha256": task.previous_sha256,
        "current_sha256": task.current_sha256,
        "answer": task.answer if task.state == "completed" else "",
        "cost_usd": task.cost_usd,
        "usage": task.usage if task.state == "completed" else {},
        "stop_reason": task.stop_reason,
        "error": task.error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
    }


def _resolve_cli() -> Any:
    """Locate the CLI once; cache on the function object."""
    cached = getattr(_resolve_cli, "_cached", None)
    if cached is None:
        cached = locate_cli()
        _resolve_cli._cached = cached  # type: ignore[attr-defined]
    return cached


def _run(task: TaskState, timeout_seconds: float) -> None:
    """Worker thread body: build a TaskRequest and run it to completion."""
    log_path = LOG_DIR / f"{task.task_id}.jsonl"
    logger = ActivityLogger(
        log_path,
        task.cwd,
        task_id=task.task_id,
        session_id=task.session_id,
    )
    try:
        task.state = "connecting"
        logger.record({"activity": "任务已开始", "status": "connecting"})

        overrides = IdentityOverrides.resolve(
            task.identity, model=task.model, effort=task.effort
        )
        # A plain ask_task may supply its own system prompt, which fully
        # overrides the identity's behavioural instructions.
        if task.system_prompt.strip():
            overrides = IdentityOverrides(
                model=overrides.model or "",
                effort=overrides.effort or "",
                # Plain tasks want the full default tool set unless the identity
                # restricted it; an explicit system_prompt means "no identity".
                allowed_tools=(),
                identity_prompt=task.system_prompt,
                canonical="",
            )

        # Compose the prompt. Re-reviews swap in the two-part protocol and
        # reuse the bound session; plain tasks keep the prompt verbatim.
        prompt_body = task.prompt
        effective_session_id = ""
        effective_resume = ""
        if task.resume_session_id:
            task.resumed = True
            if task.identity in REVIEW_IDENTITIES and task.review_target:
                resume = prepare_review_resume(
                    task.resume_session_id,
                    task.identity,
                    task.cwd,
                    task.review_target,
                )
                task.previous_sha256 = resume.previous_sha256
                task.current_sha256 = resume.current_sha256
                prompt_body = build_rereview_prompt(resume, task.prompt)
                effective_resume = task.resume_session_id
            else:
                effective_resume = task.resume_session_id
        elif task.review_target and task.identity in REVIEW_IDENTITIES:
            normalized_target = normalize_review_target(
                task.review_target, task.cwd
            )
            task.current_sha256 = target_sha256(normalized_target)
            # The reviewer needs to know which file/dir to read. Inject the
            # absolute target path as context, mirroring how the reference
            # project's task_prompt() injects the working directory. The
            # caller's prompt remains the review requirements only.
            prompt_body = (
                f"审查目标（绝对路径，请直接读取）：{normalized_target}\n\n"
                f"{task.prompt}"
            )

        request = TaskRequest(
            prompt=prompt_body,
            cwd=task.cwd,
            identity_prompt=overrides.identity_prompt,
            model=overrides.model,
            effort=overrides.effort,
            max_budget_usd=task.max_budget_usd,
            session_id=effective_session_id,
            resume_session_id=effective_resume,
            allowed_tools=overrides.allowed_tools,
            add_dirs=task.add_dirs,
            stream=True,
            timeout_seconds=timeout_seconds,
        )
        task.session_id = request.effective_session_id

        def log_event(event: dict[str, Any]) -> None:
            logger.feed(event)
            task.updated_at = time.time()

        task.state = "running"
        task.started_at = time.time()

        handle = _launch(request, log_event)
        task.handle = handle

        # Poll for completion, honoring cancellation.
        deadline = time.monotonic() + timeout_seconds
        while not handle.done.wait(timeout=0.25):
            if handle.cancel_requested:
                handle.cancel()
            if time.monotonic() > deadline + 5.0:
                # Safety net past the subprocess's own timeout.
                break

        result = handle.result
        if result is None:
            result = TaskResult()

        if handle.error is not None and not result.text:
            raise CliError(str(handle.error))

        task.answer = result.text
        task.cost_usd = result.total_cost_usd
        task.usage = result.usage
        task.model_usage = result.model_usage
        task.stop_reason = result.stop_reason
        if task.session_id and not result.session_id:
            result.session_id = task.session_id
        elif result.session_id:
            task.session_id = result.session_id

        # Persist the review binding so a later re-review can find this session.
        if (
            task.review_target
            and task.identity in REVIEW_IDENTITIES
            and task.session_id
        ):
            bind_review_session(
                session_id=task.session_id,
                identity=task.identity,
                cwd=task.cwd,
                target=task.review_target,
                baseline_sha256=task.current_sha256,
            )

        if task.cancel_requested and result.stop_reason in {"cancelled", "error", ""}:
            task.state = "cancelled"
            task.error = "任务被取消" if not task.answer else None
        elif result.is_error and not task.answer:
            task.state = "failed"
            task.error = result.api_error_status or "CLI returned an error"
        else:
            task.state = "completed"
    except Exception as exc:
        task.state = "failed"
        task.error = str(exc)
        logger.terminal(
            "任务执行失败",
            status=type(exc).__name__,
            session_id=task.session_id,
        )
    finally:
        logger.close()
        task.handle = None
        task.updated_at = time.time()
        task.finished_at = task.updated_at
        with task.condition:
            task.condition.notify_all()


def _launch(request: TaskRequest, log_event) -> TaskHandle:
    """Run synchronously on a background handle; keeps _run readable."""
    # We want the ActivityLogger to receive stream events as they happen, so
    # launch async and feed events through the callback.
    from .cli_worker import run_task_async

    return run_task_async(
        request, _resolve_cli(), event_callback=log_event
    )


@mcp.tool()
def ask_status(task_id: str = "") -> dict[str, Any]:
    """Check CLI connectivity, or inspect one dispatched task.

    With no argument, reports whether the Claude CLI is installed and runnable.
    With ``task_id``, returns the same payload as ``ask_wait`` for that task.
    """
    if task_id:
        with TASKS_LOCK:
            task = TASKS.get(task_id)
        if not task:
            return {"ok": False, "error": f"Unknown task_id: {task_id}"}
        return {"ok": True, **_public(task)}
    try:
        location = _resolve_cli()
        return {
            "ok": True,
            "connected": True,
            "endpoint": location.endpoint,
            "version": location.version,
            "event_routing": "isolated_subprocess_per_task",
        }
    except CliError as exc:
        return {"ok": False, "connected": False, "error": str(exc)}


@mcp.tool()
def ask_start(
    prompt: str,
    cwd: str = "",
    timeout_seconds: int = 300,
    model: str = "",
    effort: str = "",
    identity: str = "",
    max_budget_usd: float = 2.0,
    review_target: str = "",
    resume_session_id: str = "",
    resume_review: bool = False,
) -> dict[str, Any]:
    """Queue a worker task, optionally resuming a bound review session.

    The prompt must contain only the task body; identity instructions are
    injected by the bridge via the CLI's system prompt.
    """
    working_dir = Path(cwd or Path.cwd()).resolve()
    if not working_dir.is_dir():
        return {"ok": False, "error": f"cwd is not a directory: {working_dir}"}
    if not (prompt or "").strip():
        return {"ok": False, "error": "prompt must not be empty"}

    try:
        overrides = IdentityOverrides.resolve(
            identity, model=model, effort=effort
        )
        canonical_identity = overrides.canonical
        canonical_resume_session_id = (
            normalize_session_id(resume_session_id)
            if (resume_session_id or "").strip()
            else ""
        )
        canonical_review_target = (
            normalize_review_target(review_target, str(working_dir))
            if (review_target or "").strip()
            else ""
        )
        if resume_review and canonical_identity not in REVIEW_IDENTITIES:
            raise ValueError("resume_review 只能与 S1、S2、S3 一起使用")
        if resume_review and not canonical_resume_session_id:
            if not canonical_review_target:
                raise ValueError("自动复审时必须提供 review_target")
            canonical_resume_session_id = find_review_session(
                canonical_identity,
                str(working_dir),
                canonical_review_target,
            )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if canonical_review_target and canonical_identity not in REVIEW_IDENTITIES:
        return {
            "ok": False,
            "error": "review_target 只能与 S1、S2、S3 审查身份一起使用",
        }
    if canonical_resume_session_id and canonical_identity not in REVIEW_IDENTITIES:
        return {
            "ok": False,
            "error": "只有 S1、S2、S3 支持复用旧审查会话",
        }
    if (
        canonical_resume_session_id
        and canonical_identity in REVIEW_IDENTITIES
        and not canonical_review_target
    ):
        return {
            "ok": False,
            "error": "复用旧审查会话时必须提供 review_target",
        }

    task_id = f"gc-{uuid.uuid4().hex[:12]}"
    task = TaskState(
        task_id=task_id,
        prompt=prompt,
        cwd=str(working_dir),
        identity=canonical_identity,
        model=overrides.model,
        effort=overrides.effort,
        max_budget_usd=max_budget_usd,
        resume_session_id=canonical_resume_session_id,
        review_target=canonical_review_target,
        resume_review=bool(
            canonical_resume_session_id
            and canonical_identity in REVIEW_IDENTITIES
        ),
    )
    with TASKS_LOCK:
        TASKS[task_id] = task
    thread = threading.Thread(
        target=_run,
        args=(task, float(timeout_seconds)),
        daemon=True,
    )
    thread.start()
    return {
        "ok": True,
        "task_id": task_id,
        "state": task.state,
        "cwd": task.cwd,
        "identity": task.identity or None,
        "model": task.model or None,
        "effort": task.effort or None,
        "max_budget_usd": task.max_budget_usd,
        "resume_session_id": task.resume_session_id or None,
        "resume_review": task.resume_review,
        "review_target": task.review_target or None,
    }


@mcp.tool()
def ask_wait(task_id: str, timeout_seconds: int = 55) -> dict[str, Any]:
    """Wait briefly for a worker task; returns current state on timeout."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return {"ok": False, "error": f"Unknown task_id: {task_id}"}
    if task.state not in _TERMINAL_STATES:
        with task.condition:
            task.condition.wait(timeout=max(0, min(timeout_seconds, 55)))
    return {"ok": True, **_public(task)}


@mcp.tool()
def ask_cancel(task_id: str) -> dict[str, Any]:
    """Request cancellation of a running worker task."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return {"ok": False, "error": f"Unknown task_id: {task_id}"}
    handle = task.handle
    if not handle:
        return {
            "ok": False,
            "state": task.state,
            "error": "Task is not cancellable right now",
        }
    handle.cancel_requested = True
    handle.cancel()
    task.cancel_requested = True
    if task.state not in _TERMINAL_STATES:
        task.state = "cancelling"
    task.updated_at = time.time()
    return {"ok": True, **_public(task)}


@mcp.tool()
def ask_list() -> dict[str, Any]:
    """List tasks known to this bridge process."""
    with TASKS_LOCK:
        tasks = list(TASKS.values())
    return {"ok": True, "tasks": [_public(task) for task in tasks]}


@mcp.tool()
def ask_task(
    prompt: str,
    cwd: str = "",
    system_prompt: str = "",
    model: str = "",
    effort: str = "",
    add_dirs: list[str] | None = None,
    max_budget_usd: float = 5.0,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Run an arbitrary task on the Claude CLI (GLM-backed) to completion.

    Unlike the identity-oriented ``ask_start`` (which is tuned for the four
    named roles and restricted tool sets), this is the general-purpose entry
    point: it runs with the CLI's *full* default tool set (Bash, Edit, Read,
    Write, web search, ...) under ``--dangerously-skip-permissions``, blocks
    until the task finishes, and returns the final answer in one call.

    Use this to delegate any free-form coding, file, shell, or research task to
    a fresh isolated GLM sub-agent and get its result back synchronously.

    Args:
        prompt: The complete task. Describe the goal, constraints, and any
            paths; the worker has full tools, so it can read/edit files and run
            commands inside ``cwd``.
        cwd: Working directory the worker runs in (its sandbox). Defaults to the
            current directory. Pass the project root the task concerns.
        system_prompt: Optional custom system prompt. When given, it fully
            replaces the default identity instructions, giving you direct
            control over the worker's behaviour. Omit for plain tasks.
        model: Override the model (default glm-5.2). Use ``sonnet``/``opus`` for
            harder reasoning, ``glm-5.2`` for cheap/fast work.
        effort: Reasoning effort: low / medium / high / xhigh / max.
        add_dirs: Extra absolute directories the worker may access beyond cwd.
        max_budget_usd: Per-task USD spend cap (safety net).
        timeout_seconds: Hard wall-clock limit; the task is killed past this.

    Returns:
        The terminal task payload (same shape as ``ask_wait``) including
        ``answer`` when the task completed. The task stays queryable via
        ``ask_status``/``ask_list`` and cancellable via ``ask_cancel`` while it
        runs.
    """
    working_dir = Path(cwd or Path.cwd()).resolve()
    if not working_dir.is_dir():
        return {"ok": False, "error": f"cwd is not a directory: {working_dir}"}
    if not (prompt or "").strip():
        return {"ok": False, "error": "prompt must not be empty"}

    task_id = f"gc-{uuid.uuid4().hex[:12]}"
    task = TaskState(
        task_id=task_id,
        prompt=prompt,
        cwd=str(working_dir),
        system_prompt=system_prompt or "",
        model=model.strip(),
        effort=effort.strip(),
        max_budget_usd=max_budget_usd,
        add_dirs=tuple(add_dirs or ()),
    )
    with TASKS_LOCK:
        TASKS[task_id] = task
    thread = threading.Thread(
        target=_run,
        args=(task, float(timeout_seconds)),
        daemon=True,
    )
    thread.start()

    # Block synchronously until the task reaches a terminal state, waking
    # periodically so an external ask_cancel can still interrupt us. The wait
    # cap per iteration is what lets a long-running task stay cancellable.
    total = max(0, float(timeout_seconds))
    deadline = time.monotonic() + total + 10.0
    while task.state not in _TERMINAL_STATES and time.monotonic() < deadline:
        with task.condition:
            task.condition.wait(timeout=min(10.0, max(0.0, deadline - time.monotonic())))
    return {"ok": True, **_public(task)}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
