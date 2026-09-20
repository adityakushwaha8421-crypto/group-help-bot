"""In-process job runner: bounded, ordered per case, visible.

INLINE_JOBS=true used to turn every job into a bare asyncio.Task - no limit on how many ran at once, nothing
stopping two jobs for the same case from running together, nothing to look at when things piled up. Now:

* at most INLINE_JOB_WORKERS jobs run at the same time; the rest queue;
* jobs are ordered per LANE - one lane per case (or per Betix chat), so two jobs for one case never overlap;
  a slow case only ever occupies one worker, and its own lane;
* a job_id already waiting is not queued twice (what Redis/arq does for the multi-process setup);
* deferred jobs wait on a timer, not on a worker;
* stats() shows queue depth, running jobs and lanes for /health."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.config import get_settings
from app.utils.logging import get_logger

log = get_logger("runner")

CASE_JOBS = {
    "process_case_job",
    "post_case_job",
    "reversal_check_job",
    "refund_withdrawal_job",
    "late_evidence_job",
    "added_evidence_job",
    "pi_check_timeout_job",
}


@dataclass
class Job:
    name: str
    args: tuple
    job_id: str | None = None
    lane: str = field(default="")


def lane_for(name: str, args: tuple) -> str:
    if name in CASE_JOBS and args:
        return f"case:{args[0]}"
    if name == "betix_message_job" and args and isinstance(args[0], dict):
        return f"chat:{args[0].get('chat_id')}"
    return f"job:{name}"


class JobRunner:
    def __init__(
        self, max_workers: int | None = None, resolver: Callable[[str], Callable[..., Awaitable[Any]]] | None = None
    ):
        self.max_workers = max_workers or get_settings().inline_job_workers
        self._sem = asyncio.Semaphore(self.max_workers)
        self._resolver = resolver or self._task_function
        self._lanes: dict[str, deque[Job]] = {}
        self._lane_tasks: dict[str, asyncio.Task] = {}
        self._waiting_ids: set[str] = set()
        self._timers: set[asyncio.Task] = set()
        self.running = 0
        self.done = 0
        self.failed = 0

    @staticmethod
    def _task_function(name: str):
        from app.workers import tasks

        return getattr(tasks, name)

    # ---- submitting
    async def submit(self, name: str, *args: Any, job_id: str | None = None, defer_seconds: float = 0) -> bool:
        """Queue a job. False when the same job_id is already waiting (queued or on a timer)."""
        if job_id and job_id in self._waiting_ids:
            return False
        job = Job(name, args, job_id, lane_for(name, args))
        if job_id:
            self._waiting_ids.add(job_id)
        if defer_seconds and defer_seconds > 0:
            t = asyncio.create_task(self._later(job, defer_seconds))
            self._timers.add(t)
            t.add_done_callback(self._timers.discard)
        else:
            self._push(job)
        return True

    async def _later(self, job: Job, seconds: float) -> None:
        await asyncio.sleep(seconds)
        self._push(job)

    def _push(self, job: Job) -> None:
        # No await between here and the lane task check: a draining lane cannot slip away in between.
        self._lanes.setdefault(job.lane, deque()).append(job)
        task = self._lane_tasks.get(job.lane)
        if task is None or task.done():
            self._lane_tasks[job.lane] = asyncio.create_task(self._drain(job.lane))

    # ---- running
    async def _drain(self, lane: str) -> None:
        queue = self._lanes[lane]
        while queue:
            job = queue.popleft()
            if job.job_id:
                self._waiting_ids.discard(job.job_id)
            async with self._sem:
                self.running += 1
                try:
                    await self._resolver(job.name)({}, *job.args)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001  one job's crash never takes its lane, or the runner, down
                    self.failed += 1
                    log.exception("job failed", job=job.name, lane=lane, job_id=job.job_id)
                finally:
                    self.running -= 1
                    self.done += 1
        # synchronous with the emptiness check above: nothing can be appended in between
        self._lanes.pop(lane, None)
        self._lane_tasks.pop(lane, None)

    # ---- looking / stopping
    def stats(self) -> dict[str, int]:
        return {
            "workers": self.max_workers,
            "running": self.running,
            "queued": sum(len(q) for q in self._lanes.values()),
            "lanes": len(self._lane_tasks),
            "deferred": len(self._timers),
            "done": self.done,
            "failed": self.failed,
        }

    async def idle(self, timeout: float = 30.0) -> None:
        """Wait until nothing is queued, running or on a timer (tests, orderly shutdown)."""
        deadline = asyncio.get_event_loop().time() + timeout
        while (self._lane_tasks or self._timers) and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.01)

    async def shutdown(self) -> None:
        for t in list(self._timers):
            t.cancel()
        for t in list(self._lane_tasks.values()):
            t.cancel()
        await asyncio.gather(*self._timers, *self._lane_tasks.values(), return_exceptions=True)
        self._lanes.clear()
        self._lane_tasks.clear()
        self._timers.clear()
        self._waiting_ids.clear()


_runner: JobRunner | None = None


def get_runner() -> JobRunner:
    global _runner
    if _runner is None:
        _runner = JobRunner()
    return _runner


def reset_runner() -> None:
    global _runner
    _runner = None
