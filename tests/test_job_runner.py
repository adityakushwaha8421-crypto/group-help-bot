"""The in-process job runner: bounded, ordered per case, deferred without holding a worker, duplicate-safe,
and one crashing job never stops the others."""

import asyncio
import time

from app.workers.runner import JobRunner, lane_for


def make_runner(workers, log):
    async def job(ctx, *args):
        key = args[0] if args else "-"
        log.append(("start", key))
        await asyncio.sleep(0.03)
        log.append(("end", key))

    async def boom(ctx, *args):
        raise RuntimeError("bad job")

    return JobRunner(max_workers=workers, resolver=lambda name: boom if name == "boom" else job)


def test_lanes():
    assert lane_for("process_case_job", ("CASE-1", 3, True)) == "case:CASE-1"
    assert lane_for("post_case_job", ("CASE-1",)) == "case:CASE-1"
    assert lane_for("betix_message_job", ({"chat_id": -100, "message_id": 5},)) == "chat:-100"
    assert lane_for("followup_sweep_job", ()) == "job:followup_sweep_job"


async def test_jobs_for_one_case_never_overlap_but_other_cases_run_in_parallel():
    log = []
    r = make_runner(8, log)
    for _ in range(3):
        await r.submit("process_case_job", "A")
    await r.submit("process_case_job", "B")
    await r.idle()
    a_events = [e for e in log if e[1] == "A"]
    assert a_events == [("start", "A"), ("end", "A")] * 3  # strictly one after another
    starts = {k: i for i, (ev, k) in enumerate(log) if ev == "start"}
    assert starts["B"] < log.index(("end", "A"))  # B started while A's first job was still running


async def test_never_more_than_the_worker_count_running():
    log = []
    r = make_runner(2, log)
    for i in range(6):
        await r.submit("process_case_job", f"C{i}")
    peak, now = 0, 0
    # replay the log to find the peak concurrency
    await r.idle()
    for ev, _ in log:
        now += 1 if ev == "start" else -1
        peak = max(peak, now)
    assert peak == 2 and r.stats()["done"] == 6


async def test_deferred_jobs_wait_on_a_timer_not_a_worker():
    log = []
    r = make_runner(1, log)
    t0 = time.monotonic()
    await r.submit("process_case_job", "later", defer_seconds=0.1)
    assert r.stats()["deferred"] == 1 and r.stats()["running"] == 0
    await r.submit("process_case_job", "now")  # runs immediately: the timer holds no worker
    await asyncio.sleep(0.05)
    assert ("start", "now") in log and ("start", "later") not in log
    await r.idle()
    assert ("end", "later") in log and time.monotonic() - t0 >= 0.1


async def test_a_waiting_job_id_is_not_queued_twice():
    log = []
    r = make_runner(1, log)
    assert await r.submit("process_case_job", "A", job_id="post-A")
    await asyncio.sleep(0.01)  # let the first one start
    assert await r.submit("process_case_job", "A", job_id="post-A")  # the first is RUNNING now: allowed
    assert not await r.submit("process_case_job", "A", job_id="post-A")  # this one is still waiting: skipped
    await r.idle()
    assert log.count(("start", "A")) == 2


async def test_a_crashing_job_does_not_stop_its_lane():
    log = []
    r = make_runner(2, log)
    await r.submit("boom", "A")
    await r.submit("process_case_job", "A")
    await r.idle()
    assert ("end", "A") in log and r.stats()["failed"] == 1 and r.stats()["done"] == 2


async def test_shutdown_cancels_timers_and_reports_clean_stats():
    log = []
    r = make_runner(2, log)
    await r.submit("process_case_job", "A", defer_seconds=5)
    await r.shutdown()
    assert r.stats()["deferred"] == 0 and r.stats()["queued"] == 0
