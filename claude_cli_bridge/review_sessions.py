"""Review-session registry and re-review protocol.

Adapted from workbuddy_bridge/review_sessions.py.  The original project guarded
re-reviews by writing into WorkBuddy's own session store; here we keep an
independent JSON registry keyed by session-id, because the Claude CLI manages
its own transcripts and we only need to record the (identity, cwd, target,
baseline sha) binding plus the re-review prompt template.

What survives unchanged:
- SHA-256 of the reviewed file, so a re-review can tell the worker what changed.
- Strict matching on identity / cwd / target; a mismatch is rejected rather than
  silently starting a brand-new review (which would defeat the point).
- The two-part re-review protocol (regression check + independent incremental
  scan).

What changes:
- ``prepare_review_resume`` no longer reads WorkBuddy transcripts; it trusts the
  CLI's ``--resume`` to reload history and only supplies the re-review framing.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .identities import REVIEW_IDENTITIES

REGISTRY_FILENAME = "glm-claude-review-sessions.json"
_REGISTRY_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReviewResume:
    session_id: str
    identity: str
    cwd: str
    target: str
    previous_sha256: str | None
    current_sha256: str | None


def normalize_session_id(session_id: str) -> str:
    """Validate and normalise a CLI session id.

    The CLI accepts arbitrary UUIDs via --session-id; the bridge generates
    ``gc-<uuid>`` ids for fresh sessions, so accept either a bare UUID or the
    bridge prefix.
    """
    value = (session_id or "").strip()
    if not value:
        raise ValueError("resume_session_id 不能为空")
    if value.startswith("gc-"):
        value = value[3:]
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("resume_session_id 必须是有效的会话 UUID") from exc


def normalize_review_target(target: str, cwd: str) -> str:
    value = (target or "").strip()
    if not value:
        raise ValueError("review_target 不能为空")
    path = Path(value)
    if not path.is_absolute():
        path = Path(cwd) / path
    path = path.resolve()
    if not path.exists():
        raise ValueError(f"审查目标不存在: {path}")
    return str(path)


def target_sha256(target: str) -> str | None:
    """SHA-256 of a single reviewed file, or None for directories."""
    path = Path(target)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _config_dir() -> Path:
    return Path(
        os.environ.get("GLM_CLAUDE_CONFIG_DIR")
        or os.environ.get("WORKBUDDY_CONFIG_DIR")
        or (Path.home() / ".glm-claude-bridge")
    ).expanduser().resolve()


def registry_path() -> Path:
    return _config_dir() / REGISTRY_FILENAME


def _load_registry_unlocked() -> dict[str, dict[str, Any]]:
    path = registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取复审会话注册表: {path}") from exc
    sessions = data.get("sessions", {}) if isinstance(data, dict) else {}
    if not isinstance(sessions, dict):
        raise RuntimeError(f"复审会话注册表 sessions 字段格式错误: {path}")
    return sessions


def _write_registry_unlocked(sessions: dict[str, dict[str, Any]]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = {"version": 1, "sessions": sessions}
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _same_binding(a: dict[str, Any], identity: str, cwd: str, target: str) -> bool:
    return (
        a.get("identity") == identity
        and str(Path(str(a.get("cwd", ""))).resolve()).casefold() == cwd.casefold()
        and str(Path(str(a.get("target", ""))).resolve()).casefold()
        == target.casefold()
    )


def bind_review_session(
    session_id: str,
    identity: str,
    cwd: str,
    target: str,
    *,
    baseline_sha256: str | None = None,
) -> None:
    """Record that ``session_id`` reviewed ``target`` as ``identity``.

    Re-binding an existing session is allowed only when the binding matches,
    which is how the first review attaches baseline_sha256 on a re-review.
    """
    session_id = normalize_session_id(session_id)
    if identity not in REVIEW_IDENTITIES:
        raise ValueError(f"{identity} 不是代码审查身份")
    normalized_cwd = str(Path(cwd).resolve())
    normalized_target = normalize_review_target(target, normalized_cwd)
    now = time.time()
    with _REGISTRY_LOCK:
        sessions = _load_registry_unlocked()
        existing = sessions.get(session_id)
        if existing and not _same_binding(existing, identity, normalized_cwd, normalized_target):
            raise ValueError(f"旧会话 {session_id} 的身份、工作目录或审查目标不匹配")
        created_at = existing.get("created_at", now) if existing else now
        sessions[session_id] = {
            "identity": identity,
            "cwd": normalized_cwd,
            "target": normalized_target,
            "baseline_sha256": baseline_sha256,
            "created_at": created_at,
            "updated_at": now,
        }
        _write_registry_unlocked(sessions)


def find_review_session(identity: str, cwd: str, target: str) -> str:
    """Return the most recently updated session id bound to this triplet."""
    if identity not in REVIEW_IDENTITIES:
        raise ValueError("只有 S1、S2、S3 支持复用旧审查会话")
    normalized_cwd = str(Path(cwd).resolve())
    normalized_target = normalize_review_target(target, normalized_cwd)
    with _REGISTRY_LOCK:
        sessions = _load_registry_unlocked()
    matches = [
        (sid, binding)
        for sid, binding in sessions.items()
        if _same_binding(binding, identity, normalized_cwd, normalized_target)
    ]
    if not matches:
        raise ValueError(
            f"没有找到 {identity} 对该目标的旧审查会话；"
            "拒绝把“复审”悄悄改成全新审查"
        )
    matches.sort(key=lambda item: float(item[1].get("updated_at") or 0), reverse=True)
    return normalize_session_id(matches[0][0])


def prepare_review_resume(
    session_id: str,
    identity: str,
    cwd: str,
    target: str,
) -> ReviewResume:
    """Validate a re-review and capture baseline/current SHA-256.

    Unlike the reference project we do not parse a transcript here: the CLI's
    --resume reloads the prior conversation, so the registry binding is the only
    proof we need that the session belongs to this identity/target.
    """
    session_id = normalize_session_id(session_id)
    if identity not in REVIEW_IDENTITIES:
        raise ValueError("只有 S1、S2、S3 支持复用旧审查会话")
    normalized_cwd = str(Path(cwd).resolve())
    normalized_target = normalize_review_target(target, normalized_cwd)
    with _REGISTRY_LOCK:
        sessions = _load_registry_unlocked()
        existing = sessions.get(session_id)
    if not existing:
        raise ValueError(
            f"会话 {session_id} 未在复审注册表中登记，拒绝续接未知会话"
        )
    if not _same_binding(existing, identity, normalized_cwd, normalized_target):
        raise ValueError(
            f"旧会话 {session_id} 与 {identity} / 当前目录 / 当前目标不匹配，"
            "拒绝把复审串入错误会话"
        )
    return ReviewResume(
        session_id=session_id,
        identity=identity,
        cwd=normalized_cwd,
        target=normalized_target,
        previous_sha256=existing.get("baseline_sha256"),
        current_sha256=target_sha256(normalized_target),
    )


def build_rereview_prompt(resume: ReviewResume, task_prompt: str) -> str:
    """Two-part re-review protocol (regression + incremental)."""
    previous = resume.previous_sha256 or "未知（旧会话首次纳入绑定）"
    current = resume.current_sha256 or "不适用（目标不是单个文件）"
    return f"""这是对同一审查目标的复审。你必须重新读取当前完整目标，不能只机械核对自己上一轮提过的问题。

请严格执行两个相互独立的部分：
1. 回归检查：从本会话上一轮审查结论中提取所有问题，逐项结合当前代码证据标记为“已修复 / 部分修复 / 未修复 / 无法验证”。
2. 增量检查：暂时忽略上一轮问题清单，按照你的完整身份职责，从头审查当前完整目标，找出本次修改引入的新问题以及上一轮遗漏的问题，并与回归项去重。

输出顺序：
- 先输出回归检查结果；
- 再输出增量检查结果；
- 最后给出本轮结论；
- 每个问题必须包含当前代码位置和依据；
- 只审查，不修改任何文件。

审查目标：{resume.target}
上一轮文件 SHA-256：{previous}
当前文件 SHA-256：{current}
本轮补充要求：{task_prompt.strip() or "无"}
"""
