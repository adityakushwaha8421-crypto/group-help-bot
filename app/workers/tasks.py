"""arq worker: background jobs + cron sweeper + startup recovery.
Run:  arq app.workers.tasks.WorkerSettings"""

from __future__ import annotations

from arq import cron
from arq.connections import RedisSettings

from app.cases import manager
from app.config import get_settings
from app.db.models import CaseStatus
from app.db.repository import get_case, list_evidence, list_open_cases
from app.db.session import session_scope
from app.evidence.files import purge_expired
from app.followups.scheduler import sweep
from app.followups.service import schedule_case_followups
from app.telegram.betix_monitor import IncomingGroupMessage, handle_group_message
from app.utils.logging import configure_logging, get_logger
from app.workers.queue import enqueue

log = get_logger("worker")


def poster_factory():
    return manager.get_poster()


async def process_case_job(ctx, case_id: str, version: int = 0, force: bool = False, force_send: bool = False) -> str:
    async with session_scope() as session:
        case = await get_case(session, case_id)
        if case is None:
            return "missing"
        if version and case.processing_version != version:
            return "stale"  # a newer message arrived; that message's job will handle it
        outcome = await manager.process_case(session, case_id, force=force, force_send=force_send)
    log.info("process_case", case_id=case_id, outcome=outcome)
    if outcome == "deferred":
        s = get_settings()
        await enqueue(
            "process_case_job",
            case_id,
            version,
            force,
            job_id=f"process-{case_id}-{version}-r",
            defer_seconds=max(5, s.case_debounce_seconds // 2),
        )
    elif outcome == "ready":
        await enqueue("post_case_job", case_id, job_id=f"post-{case_id}")
    return outcome


async def post_case_job(ctx, case_id: str) -> str:
    async with session_scope() as session:
        outcome = await manager.post_case_to_betix(session, case_id, poster_factory())
    log.info("post_case", case_id=case_id, outcome=outcome)
    return outcome


async def reversal_check_job(ctx, case_id: str) -> str:
    """Betix reversed a withdrawal: read the payout and ask the operator to approve the refund."""
    outcome = await manager.reversal_check(case_id)
    log.info("reversal_check", case_id=case_id, outcome=outcome)
    return outcome


async def refund_withdrawal_job(ctx, case_id: str) -> str:
    """The operator approved the refund: do it in Illunise payouts, then report (see manager)."""
    outcome = await manager.refund_approved_withdrawal(case_id)
    log.info("refund_withdrawal", case_id=case_id, outcome=outcome)
    return outcome


async def late_evidence_job(ctx, case_id: str) -> int:
    async with session_scope() as session:
        case = await get_case(session, case_id)
        if not case or not case.betix_root_message_id:
            return 0
        poster = poster_factory()
        n = await manager.post_late_evidence(session, case, poster)
        if await manager.post_statement_password(session, case, poster):
            n += 1
        return n


async def added_evidence_job(ctx, case_id: str) -> int:
    """Post everything added with /add as replies to the case's screenshot in the Betix group."""
    async with session_scope() as session:
        case = await get_case(session, case_id)
        if case is None:
            return 0
        return await manager.post_added_evidence(session, case, poster_factory())


async def betix_message_job(ctx, payload: dict) -> dict:
    msg = IncomingGroupMessage.from_dict(payload)
    async with session_scope() as session:
        result = await handle_group_message(session, msg)
    if result.get("action") == "pi_ready":  # the /pi UPI check picked the order: post it now
        await enqueue("post_case_job", result["case_id"], job_id=f"post-{result['case_id']}")
    return result


async def pi_check_timeout_job(ctx, case_id: str, order_id: str | None = None) -> str:
    """The wait for the Betix bot's answer to `/pi <order_id>` is over (order_id None: the current query)."""
    async with session_scope() as session:
        if order_id is None:
            case = await get_case(session, case_id)
            asked, _ = await manager.pi_answers(session, case) if case else ([], {})
            order_id = asked[-1] if asked else ""
        outcome = await manager.resolve_pi_check(session, case_id, timed_out=order_id)
    log.info("pi check timeout", case_id=case_id, outcome=outcome)
    if outcome == "ready":
        await enqueue("post_case_job", case_id, job_id=f"post-{case_id}")
    return outcome


async def followup_sweep_job(ctx) -> dict:
    return await sweep(poster_factory)


async def retention_job(ctx) -> int:
    return purge_expired()


async def recover(ctx=None) -> dict:
    """Restart recovery: resume unfinished workflows without duplicating side effects."""
    counts: dict[str, int] = {}
    async with session_scope() as session:
        for case in await list_open_cases(session):
            st = case.status
            if st in (CaseStatus.ANALYZING_EVIDENCE.value, CaseStatus.SEARCHING_ORDER.value):
                from app.cases.state_machine import transition

                await transition(
                    session, case, CaseStatus.WAITING_FOR_INPUT, reason="recovery: restart mid-processing", strict=False
                )
                case.processing_version += 1
                await enqueue(
                    "process_case_job",
                    case.case_id,
                    case.processing_version,
                    True,
                    job_id=f"recover-process-{case.case_id}-{case.processing_version}",
                )
                counts["reprocess"] = counts.get("reprocess", 0) + 1
            elif st == CaseStatus.WAITING_FOR_INPUT.value:
                ev = await list_evidence(session, case.case_id)
                from app.cases.correlation import hard_missing, missing_items
                from app.utils.timeutil import utcnow

                missing = missing_items(case, ev)
                s = get_settings()
                if missing and not hard_missing(missing) and s.force_send_seconds > 0:
                    waited = (utcnow() - case.last_input_at).total_seconds() if case.last_input_at else 1e9
                    case.processing_version += 1
                    await enqueue(
                        "process_case_job",
                        case.case_id,
                        case.processing_version,
                        True,
                        True,
                        job_id=f"recover-force-{case.case_id}-{case.processing_version}",
                        defer_seconds=max(0, int(s.force_send_seconds - waited)),
                    )
                    counts["force_send"] = counts.get("force_send", 0) + 1
                elif not missing:
                    # complete when we went down but never processed: run it now
                    case.processing_version += 1
                    await enqueue(
                        "process_case_job",
                        case.case_id,
                        case.processing_version,
                        True,
                        job_id=f"recover-process-{case.case_id}-{case.processing_version}",
                    )
                    counts["process"] = counts.get("process", 0) + 1
            elif st == CaseStatus.CHECKING_ORDER_UPI.value:
                s = get_settings()
                await enqueue(
                    "pi_check_timeout_job",
                    case.case_id,
                    job_id=f"recover-pi-{case.case_id}",
                    defer_seconds=s.pi_check_timeout_seconds,
                )
                counts["pi_check"] = counts.get("pi_check", 0) + 1
            elif st in (CaseStatus.READY_FOR_BETIX.value, CaseStatus.POSTED_TO_BETIX.value):
                await enqueue("post_case_job", case.case_id, job_id=f"recover-post-{case.case_id}")
                counts["post"] = counts.get("post", 0) + 1
            elif st in (
                CaseStatus.WAITING_FOR_CONFIRMATION.value,
                CaseStatus.FOLLOWUP_1_SENT.value,
                CaseStatus.FOLLOWUP_2_SENT.value,
            ):
                if not case.followup_cancelled:
                    await schedule_case_followups(session, case)  # no-op if rows exist (unique constraint)
                counts["monitoring"] = counts.get("monitoring", 0) + 1
    log.info("recovery complete", **counts)
    if get_settings().betix_monitor_mode == "user":
        try:
            from app.telegram.betix_monitor import backfill_user_mode

            counts["backfilled"] = await backfill_user_mode()
        except Exception as exc:  # noqa: BLE001
            log.warning("backfill skipped", error=repr(exc))
    return counts


async def startup(ctx) -> None:
    configure_logging(get_settings().log_level)
    await recover(ctx)


async def shutdown(ctx) -> None:
    from app.db.session import dispose

    await dispose()


class WorkerSettings:
    functions = [
        process_case_job,
        post_case_job,
        reversal_check_job,
        refund_withdrawal_job,
        late_evidence_job,
        added_evidence_job,
        betix_message_job,
        pi_check_timeout_job,
        followup_sweep_job,
        retention_job,
    ]
    cron_jobs = [
        cron(followup_sweep_job, second={0, 30}, run_at_startup=True),
        cron(retention_job, hour={3}, minute={15}),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 5
    job_timeout = 900
    keep_result = 3600
    retry_jobs = True
    max_tries = 3
