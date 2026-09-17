"""Sweeper that executes due follow-ups. Called by the arq cron every 30s (or by an in-process loop when
INLINE_JOBS=true). Each row is handled in its own transaction so one failure never blocks the rest."""

from __future__ import annotations

import asyncio

from app.db.repository import due_followups
from app.db.session import session_scope
from app.followups.service import ESCALATION_NUMBER, WAITING, execute_followup
from app.utils.logging import get_logger

log = get_logger("followups.sweeper")
HEARTBEAT_PASSES = 20  # one "alive" line every 20 passes (10 minutes at the default 30s interval)


async def sweep(poster_factory) -> dict[str, int]:
    results: dict[str, int] = {}
    async with session_scope() as session:
        due = await due_followups(session)
        # Several reminders overdue for ONE case means the bot was offline through them. Posting "Any update?"
        # two or three times in a row looks like spam, so send only the latest and drop the older ones.
        from sqlalchemy import select

        from app.db.models import Case

        waiting = set(
            (
                await session.execute(
                    select(Case.case_id).where(
                        Case.case_id.in_({r.case_id for r in due}),
                        Case.status.in_(WAITING),
                        Case.followup_cancelled.is_(False),
                    )
                )
            ).scalars()
        )  # a confirmed / closed case is left to execute_followup, which skips every row
        latest: dict[str, int] = {}
        for r in due:
            if r.number != ESCALATION_NUMBER and r.case_id in waiting:
                latest[r.case_id] = max(latest.get(r.case_id, 0), r.number)
        ids = []
        for r in due:
            if r.case_id in latest and r.number != ESCALATION_NUMBER and r.number < latest[r.case_id]:
                r.status = "cancelled"
                r.error = f"missed while the bot was offline; replaced by follow-up #{latest[r.case_id]}"
                results["collapsed"] = results.get("collapsed", 0) + 1
                log.info(
                    "overdue follow-up collapsed", case_id=r.case_id, number=r.number, sent_instead=latest[r.case_id]
                )
                continue
            ids.append((r.id, r.case_id, r.number))
    for row_id, case_id, number in ids:
        try:
            async with session_scope() as session:
                from sqlalchemy import select

                from app.db.models import Followup

                fu = (await session.execute(select(Followup).where(Followup.id == row_id))).scalar_one()
                if fu.status != "scheduled":
                    continue
                poster = poster_factory() if number != 99 else None
                outcome = await execute_followup(session, fu, poster)
                results[outcome] = results.get(outcome, 0) + 1
        except Exception as exc:  # noqa: BLE001
            log.error("followup sweep error", case_id=case_id, number=number, error=repr(exc))
            results["error"] = results.get("error", 0) + 1
    if results:
        log.info("followup sweep", **results)
    return results


async def run_forever(poster_factory, interval_seconds: int = 30, stop_event: asyncio.Event | None = None) -> None:
    """The reminder heartbeat. Runs for the life of the process; a crash in one pass never ends the loop.

    It says so in the log when it starts and every HEARTBEAT_PASSES passes after that, so "reminders stopped"
    can be told apart from "nothing was due" without guessing."""
    log.info("followup sweeper started", every_seconds=interval_seconds)
    passes = 0
    try:
        while not (stop_event and stop_event.is_set()):
            passes += 1
            try:
                await sweep(poster_factory)
            except Exception as exc:  # noqa: BLE001
                log.error("sweeper loop error", error=repr(exc))
            if passes % HEARTBEAT_PASSES == 0:
                log.info("followup sweeper alive", passes=passes)
            try:
                await asyncio.wait_for(
                    stop_event.wait() if stop_event else asyncio.sleep(interval_seconds), timeout=interval_seconds
                )
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        raise
    except BaseException as exc:  # the loop must never die quietly: a dead loop means no reminders at all
        log.error("followup sweeper stopped", error=repr(exc), passes=passes)
        raise
    log.info("followup sweeper stopped", passes=passes)
