"""Background job runner for the sidecar.

The pipeline functions are blocking and print progress to stdout. We run them on
a single background worker (one heavy job at a time — realistic for a local
machine where ffmpeg/LLM would contend anyway) and capture stdout so the app can
stream stage/progress over a WebSocket. No pipeline code is modified: we tee
stdout and parse the existing ``[2/4] Phase 1: ...`` style lines.
"""

from __future__ import annotations

import io
import re
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from typing import Callable

# ``[2/4] Phase 1: Reviewing clips with gemini...`` → (2, 4, "Phase 1: ...")
_STAGE_RE = re.compile(r"\[(\d+)\s*/\s*(\d+)\]\s*(.+)")


@dataclass
class Job:
    id: str
    kind: str  # "create" | "analyze" | "cut"
    project: str
    status: str = "queued"  # queued | running | completed | failed
    stage: str | None = None
    progress: float | None = None
    error: str | None = None
    result: dict | None = None
    project_root: object = None  # Path; used to read live cost from traces.jsonl
    log_tail: deque = field(default_factory=lambda: deque(maxlen=200))
    duration_sec: float | None = None  # wall-clock, set on completion
    # Monotonic revision bumped on every state change so WS clients can detect updates.
    rev: int = 0

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "project": self.project,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "error": self.error,
            "result": self.result,
            "log_tail": list(self.log_tail),
            "duration_sec": self.duration_sec,
            "rev": self.rev,
        }


class _TeeParser(io.TextIOBase):
    """Writes through to the real stdout AND feeds the owning job's progress."""

    def __init__(self, job: Job, registry: "JobRegistry", original):
        self._job = job
        self._registry = registry
        self._original = original
        self._buf = ""

    def write(self, s: str) -> int:
        try:
            self._original.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._consume_line(line)
        return len(s)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass

    def _consume_line(self, line: str):
        clean = line.rstrip()
        if not clean:
            return
        # Strip ANSI color codes the CLI emits.
        clean = re.sub(r"\x1b\[[0-9;]*m", "", clean)
        self._job.log_tail.append(clean)
        m = _STAGE_RE.search(clean)
        if m:
            cur, total, label = int(m.group(1)), int(m.group(2)), m.group(3).strip()
            self._job.stage = label
            self._job.progress = max(0.0, min(1.0, (cur - 1) / total)) if total else None
        self._registry._touch(self._job)


class JobRegistry:
    """In-process registry. Single worker thread executes jobs FIFO."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: "Queue[Job]" = Queue()
        self._listeners: dict[str, list[Queue]] = {}
        self._worker = threading.Thread(target=self._run_loop, daemon=True, name="vx-job-worker")
        self._worker.start()

    # -- public API ---------------------------------------------------------
    def submit(self, kind: str, project: str, project_root, fn: Callable[[], dict]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, project=project, project_root=project_root)
        job._fn = fn  # type: ignore[attr-defined]
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        self._touch(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def subscribe(self, job_id: str) -> Queue:
        q: Queue = Queue()
        with self._lock:
            self._listeners.setdefault(job_id, []).append(q)
            job = self._jobs.get(job_id)
        if job:
            q.put(job.snapshot())  # prime with current state
        return q

    def unsubscribe(self, job_id: str, q: Queue):
        with self._lock:
            lst = self._listeners.get(job_id)
            if lst and q in lst:
                lst.remove(q)

    # -- internals ----------------------------------------------------------
    def _touch(self, job: Job):
        job.rev += 1
        snap = job.snapshot()
        with self._lock:
            listeners = list(self._listeners.get(job.id, []))
        for q in listeners:
            q.put(snap)

    def _run_loop(self):
        while True:
            job = self._queue.get()
            job.status = "running"
            self._touch(job)
            original = sys.stdout
            tee = _TeeParser(job, self, original)
            start = time.monotonic()
            try:
                with redirect_stdout(tee):
                    result = job._fn()  # type: ignore[attr-defined]
                job.result = result if isinstance(result, dict) else {"ok": True}
                job.status = "completed"
                job.progress = 1.0
            except Exception as exc:  # surface to the client; never crash the worker
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
            finally:
                job.duration_sec = round(time.monotonic() - start, 3)
                self._record_timing(job)
                self._touch(job)

    def _record_timing(self, job: Job):
        """Persist the job's wall-clock so the app can show honest durations."""
        if job.project_root is None:
            return
        try:
            from ..tracing import record_stage_timing

            record_stage_timing(
                Path(job.project_root),
                stage=f"job:{job.kind}",
                seconds=job.duration_sec or 0.0,
                meta={"status": job.status, "job_id": job.id},
            )
        except Exception:
            pass  # timing must never break a job


# A single registry shared across the app process.
REGISTRY = JobRegistry()
