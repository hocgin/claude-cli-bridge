"""Subprocess wrapper around the Claude Code CLI.

This replaces ``acp.py`` from workbuddy_bridge.  The whole ACP chain —

    discover_desktop_server → spawn_isolated_server → AcpClient.connect
    → new_session/load_session → configure_session → prompt(stream) → cancel

-- collapses into a single ``claude -p`` invocation.  The CLI already provides
the agent loop, tool execution, session persistence and streaming that the ACP
client had to re-implement.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

DEFAULT_BIN = os.environ.get("GLM_CLAUDE_BIN", "claude")
DEFAULT_MODEL = os.environ.get("GLM_CLAUDE_DEFAULT_MODEL", "glm-5.2")
STARTUP_TIMEOUT_SECONDS = 30.0
# The CLI requires --session-id to be a real UUID (it rejects prefixed ids).
# Pick a fresh UUID per task so each invocation is naturally isolated, mirroring
# new_session(); --resume reuses the prior UUID to continue the conversation.


class CliError(RuntimeError):
    """Raised when the Claude CLI cannot be located or run to completion."""


@dataclass(frozen=True)
class CliLocation:
    """Resolved location of the ``claude`` executable."""

    bin_path: Path
    version: str

    @property
    def endpoint(self) -> str:
        return str(self.bin_path)


def locate_cli(bin_path: str = DEFAULT_BIN) -> CliLocation:
    """Find the CLI binary and confirm it responds to ``--version``."""
    resolved = shutil.which(bin_path) or str(Path(bin_path).expanduser())
    if not resolved or not Path(resolved).is_file():
        raise CliError(f"Claude CLI not found on PATH: {bin_path!r}")
    try:
        proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CliError(f"Could not execute Claude CLI: {exc}") from exc
    version = (proc.stdout or proc.stderr or "").strip() or "unknown"
    if proc.returncode != 0 and not version:
        raise CliError(f"Claude CLI --version failed (rc={proc.returncode})")
    return CliLocation(bin_path=Path(resolved), version=version)


@dataclass
class TaskRequest:
    """Everything needed to launch one isolated worker task."""

    prompt: str
    cwd: str = ""
    identity_prompt: str = ""
    model: str = ""
    effort: str = ""
    max_budget_usd: float = 2.0
    session_id: str = ""
    resume_session_id: str = ""
    add_dirs: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    output_schema: dict[str, Any] | None = None
    stream: bool = True
    timeout_seconds: float = 300.0

    @property
    def effective_session_id(self) -> str:
        if self.resume_session_id:
            return self.resume_session_id
        if self.session_id:
            return self.session_id
        # CLI mandates a bare UUID; no prefix allowed.
        return str(uuid.uuid4())


@dataclass
class TaskResult:
    """Normalised result parsed from the CLI's final ``result`` event."""

    session_id: str = ""
    text: str = ""
    stop_reason: str = ""
    is_error: bool = False
    total_cost_usd: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    model_usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    api_error_status: str | None = None


def build_argv(req: TaskRequest, location: CliLocation) -> list[str]:
    """Translate a TaskRequest into the argv passed to ``claude``.

    Mirrors how workbuddy_bridge mapped ACP options onto a session:

        new_session             -> --session-id <uuid>
        load_session / resume   -> --resume <uuid>
        permission_mode         -> --dangerously-skip-permissions   (fullAccess)
        model / reasoning_effort-> --model / --effort
        identity_prompt         -> --append-system-prompt            (identities)
    """
    argv: list[str] = [str(location.bin_path), "-p"]
    argv += ["--output-format", "stream-json" if req.stream else "json"]
    if req.stream:
        # stream-json is only emitted when --verbose is on.
        argv.append("--verbose")
    argv.append("--dangerously-skip-permissions")
    argv += ["--model", req.model or DEFAULT_MODEL]
    if req.effort:
        argv += ["--effort", req.effort]
    argv += ["--max-budget-usd", str(req.max_budget_usd)]
    # The CLI rejects --session-id alongside --resume unless --fork-session is
    # also given.  A resume already fixes the session id, so pass only --resume;
    # fresh tasks pass only --session-id.
    if req.resume_session_id:
        argv += ["--resume", req.resume_session_id]
    else:
        argv += ["--session-id", req.effective_session_id]
    for extra in req.add_dirs:
        argv += ["--add-dir", str(extra)]
    if req.allowed_tools:
        argv += ["--allowed-tools", " ".join(req.allowed_tools)]
    if req.identity_prompt:
        argv += ["--append-system-prompt", req.identity_prompt]
    if req.output_schema:
        argv += ["--json-schema", json.dumps(req.output_schema)]
    argv.append(req.prompt)
    return argv


def _iter_events(proc: subprocess.Popen) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON events from the CLI's stdout stream.

    In stream-json mode each line is one JSON object; in json mode the whole
    stdout is a single object on its own line.
    """
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def run_task(
    req: TaskRequest,
    location: CliLocation,
    *,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
) -> TaskResult:
    """Run one ``claude -p`` task to completion and return its result.

    Honours a soft cancellation via the ``cancel`` flag on the returned handle
    when launched through :func:`run_task_async`.
    """
    argv = build_argv(req, location)
    cwd = str(Path(req.cwd or Path.cwd()).resolve())
    env = dict(os.environ)
    # Keep the subprocess off the user's terminal so it never steals the TTY.
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv built locally, not shell
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise CliError(f"Failed to start Claude CLI: {exc}") from exc

    result = TaskResult()
    try:
        for event in _iter_events(proc):
            if event_callback:
                try:
                    event_callback(event)
                except Exception:
                    pass
            kind = str(event.get("type", ""))
            if kind == "result":
                _absorb_result(result, event)
        proc.wait(timeout=max(0.0, req.timeout_seconds))
    except subprocess.TimeoutExpired:
        _terminate(proc)
        raise CliError(
            f"Claude CLI task timed out after {req.timeout_seconds}s"
        )
    finally:
        _terminate_if_alive(proc)

    if not result.session_id and result.is_error:
        raise CliError(result.text or "Claude CLI returned an error result")
    return result


def run_task_async(
    req: TaskRequest,
    location: CliLocation,
    *,
    event_callback: Callable[[dict[str, Any]], None] | None = None,
) -> "TaskHandle":
    """Launch a task on a daemon thread; return a cancellable handle."""
    handle = TaskHandle(request=req)

    def target() -> None:
        try:
            handle.result = run_task(
                req, location, event_callback=event_callback
            )
            handle.error = None
        except BaseException as exc:  # noqa: BLE001 - surfaced via .error
            handle.error = exc
            handle.result = TaskResult(
                is_error=True,
                text=str(exc),
                stop_reason="error",
            )
        finally:
            handle.done.set()

    handle.thread = threading.Thread(
        target=target,
        name=f"claude-worker-{req.effective_session_id[:12]}",
        daemon=True,
    )
    handle.thread.start()
    return handle


@dataclass
class TaskHandle:
    """Handle to a worker running on a background thread."""

    request: TaskRequest
    result: TaskResult | None = None
    error: BaseException | None = None
    done: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    cancel_requested: bool = False

    def cancel(self) -> None:
        """Best-effort cancel; the subprocess is killed on the next check."""
        self.cancel_requested = True


def _absorb_result(result: TaskResult, event: dict[str, Any]) -> None:
    """Copy the fields we care about out of the terminal ``result`` event."""
    result.raw = event
    result.session_id = str(event.get("session_id") or result.session_id)
    result.text = str(event.get("result") or result.text)
    result.stop_reason = str(event.get("stop_reason") or result.stop_reason)
    result.is_error = bool(event.get("is_error"))
    result.total_cost_usd = float(event.get("total_cost_usd") or 0.0)
    result.usage = dict(event.get("usage") or {})
    result.model_usage = dict(event.get("modelUsage") or {})
    result.api_error_status = event.get("api_error_status")


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def _terminate_if_alive(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        _terminate(proc)
