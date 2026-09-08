"""Small orchestration-layer progress recorder.

The recorder is intentionally outside ``generation``.  It measures the
callers that coordinate extraction, projection, and reporting without adding
time, filesystem, or process dependencies to deterministic semantic code.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ProgressRecorder:
    """Append-only JSONL stage events for one orchestration run."""

    def __init__(self, path: Path, *, project: str, run_id: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.project = project
        self.run_id = run_id or self.path.parent.name
        self.pid = os.getpid()

    def _append(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()

    @contextmanager
    def stage(
        self,
        function: str,
        stage: str,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Record a start event and a completion event around one stage.

        The start event deliberately has no end time.  If a process is killed
        or hangs, the last pending start event identifies the exact function
        and stage that had not emitted a completion event.
        """
        started_at = _now()
        started_ns = time.perf_counter_ns()
        token: dict[str, Any] = {
            "status": None,
            "metadata": dict(metadata or {}),
        }
        common = {
            "schema_version": 1,
            "run_id": self.run_id,
            "project": self.project,
            "pid": self.pid,
            "function": function,
            "stage": stage,
            "started_at": started_at,
            "metadata": token["metadata"],
        }
        self._append({
            **common,
            "phase": "start",
            "ended_at": None,
            "duration_ms": None,
            "status": "RUNNING",
            "completion": "PENDING",
            "complete": False,
        })
        try:
            yield token
        except Exception as exc:
            ended_at = _now()
            self._append({
                **common,
                "phase": "end",
                "ended_at": ended_at,
                "duration_ms": round((time.perf_counter_ns() - started_ns) / 1_000_000, 3),
                "status": "FAILED",
                "completion": "FAILED",
                "complete": False,
                "error": f"{type(exc).__name__}: {exc}",
                "metadata": token["metadata"],
            })
            raise
        else:
            ended_at = _now()
            status = str(token.get("status") or "COMPLETED")
            self._append({
                **common,
                "phase": "end",
                "ended_at": ended_at,
                "duration_ms": round((time.perf_counter_ns() - started_ns) / 1_000_000, 3),
                "status": status,
                "completion": "COMPLETED" if status == "COMPLETED" else status,
                "complete": True,
                "metadata": token["metadata"],
            })


__all__ = ["ProgressRecorder"]
