"""Append-only JSONL activity log, one file per task.

Mirrors workbuddy_bridge/activity_log.ActivityLogger: every CLI stream event is
appended as one JSON line so a task's full run is replayable from disk.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class ActivityLogger:
    def __init__(
        self,
        path: Path,
        cwd: str = "",
        *,
        task_id: str = "",
        session_id: str = "",
    ) -> None:
        self.path = Path(path)
        self.cwd = cwd
        self.task_id = task_id
        self.session_id = session_id
        self._lock = threading.Lock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Open in binary append; we encode each record ourselves.
        self._handle = self.path.open("ab", buffering=0)

    def _write(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", time.time())
        with self._lock:
            if self._closed:
                return
            line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
            self._handle.write(line)

    def feed(self, event: dict[str, Any]) -> None:
        """Append a raw CLI stream event."""
        self._write({"event": event})

    def record(self, record: dict[str, Any]) -> None:
        """Append a bridge-level lifecycle record."""
        if self.task_id:
            record.setdefault("task_id", self.task_id)
        if self.session_id:
            record.setdefault("session_id", self.session_id)
        if self.cwd:
            record.setdefault("cwd", self.cwd)
        self._write(record)

    def terminal(self, message: str, **fields: Any) -> None:
        self.record({"activity": message, "status": "terminal", **fields})

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._handle.flush()
            except (OSError, ValueError):
                pass
            try:
                self._handle.close()
            except (OSError, ValueError):
                pass
