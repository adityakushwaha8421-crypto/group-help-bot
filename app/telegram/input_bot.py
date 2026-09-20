"""Telegram input bot: you send screenshot / registration / statement / video (in any order, as separate
messages) and the bot groups them into one case, then triggers processing."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    Message,
)
from sqlalchemy.exc import IntegrityError

from app.cases import manager
from app.cases.correlation import (
    ForwardOrigin,
    IncomingInput,
    attach_message,
    hard_missing,
    missing_items,
    statement_needs_password,
)
from app.config import get_settings
from app.db.models import KIND_WITHDRAWAL, CaseStatus
from app.db.repository import (
    case_evidence_requested,
    find_case_by_order_id,
    get_case,
    get_case_for_update,
    latest_case_for_user,
    list_evidence,
    list_open_cases,
)
from app.db.session import session_scope
from app.telegram import add_evidence, notifications, order_search
from app.telegram.betix_monitor import build_router as build_betix_router
from app.telegram.throttle import get_throttle
from app.telegram.ui import b, card, case_label, code, esc, i, join_and, kv, para, title
from app.utils.logging import get_logger
from app.workers.queue import enqueue

log = get_logger("input_bot")
router = Router(name="input")
router.message.filter(F.chat.type == "private")  # commands and evidence intake exist ONLY in private chat


UNAUTHORIZED_COOLDOWN_SECONDS = 3600
_refused_at: dict[int, float] = {}


def _allowed(message: Message) -> bool:
    s = get_settings()
    return bool(message.from_user and message.from_user.id in s.admin_user_ids)


@router.message.middleware()
async def allowlist_middleware(
    handler: Callable[[Message, dict[str, Any]], Awaitable[Any]], event: Message, data: dict[str, Any]
):
    if event.chat.type != "private":
        return await handler(event, data)  # group traffic is handled by the betix router
    if not _allowed(event) and not get_settings().allow_customer_direct_submissions:
        uid = event.from_user.id if event.from_user else 0
        log.warning("unauthorized submitter", user_id=uid)
        now = time.monotonic()
        if now - _refused_at.get(uid, -1e9) > UNAUTHORIZED_COOLDOWN_SECONDS:
            # say it once an hour, not once per message: an unauthorized chatter (or another bot) must never
            # be able to make this bot flood its chat
            _refused_at[uid] = now
            await event.answer(
                card(
                    title("⛔", "Not authorized"),
                    [esc("Ask the admin to add your Telegram user ID to ADMIN_TELEGRAM_USER_IDS.")],
                ),
                parse_mode="HTML",
            )
        return None
    return await handler(event, data)


def help_text() -> str:
    q = get_settings().collection_seconds
    secs = get_settings().force_send_seconds
    return para(
        f"👋 {b('Payment Verification Bot')}",
        "📥 Just send the evidence in any order and I'll open a case:\n"
        "📸 payment screenshot · 📱 customer's mobile · 📄 bank statement · 🎬 payment video",
        "\U0001f4b8 "
        + b("Withdrawal")
        + ": send the withdrawal id ("
        + code("WD-84425-67115")
        + ") and the bank statement.\n"
        "I find it in Illunise payouts and check the statement is for the SAME bank account.\n"
        "\u2705 Match: I post " + code("BXWD-84425-67115") + " to Betix with the statement as its reply.\n"
        "\u274c No match: nothing is sent \u2014 I tell you and keep it for manual review.",
        "\u2139\ufe0f "
        + b("Good to know")
        + "\n"
        + i(f"\u23f1 Everything sent within {q}s becomes one case.")
        + "\n"
        + i("\U0001f522 The screenshot must show the UTR.")
        + "\n"
        + i(f"\U0001f4e4 Missing statement or video? I still send to Betix after {secs}s.")
        + "\n"
        + i(f"\u2795 Add the rest later with {code('/add')}."),
        f"⚙️ {b('Commands')}\n"
        f"{code('/add MOBILE')} — add evidence to a case already in Betix\n"
        f"{code('/search [MOBILE]')} — find the order id for a screenshot, nothing is posted\n"
        f"{code('/status [ORDER-ID]')} — where a case stands\n"
        f"{code('/cases')} — all open cases\n"
        f"{code('/summary [DAYS]')} — overview by stage\n"
        f"{code('/cancel')} — discard what's in progress\n"
        f"{code('/restart')} — pull the latest code and restart",
    )


@router.message(Command("start", "help"))
async def cmd_help(message: Message):
    await message.answer(help_text(), parse_mode="HTML")


@router.message(Command("restart"))
async def cmd_restart(message: Message):
    """Restart the bot process. Cases, follow-ups and queued jobs live in the database and carry on."""
    from app import restart

    if restart.is_stale(message.date.timestamp()):
        return  # the /restart that started this very process, delivered again
    _, code_line = await restart.pull_latest()  # the newest code from GitHub, when it can be fast-forwarded
    await message.answer(
        para(
            "\U0001f504 " + b("RESTARTING"),
            ["\U0001f4e6 Code: " + esc(code_line), "\u23f3 Back in a few seconds..."],
            i("Open cases and follow-ups are kept."),
        ),
        parse_mode="HTML",
    )
    asyncio.get_running_loop().call_later(1.0, restart.request, message.chat.id)


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject):
    """Stand-alone order-id lookup: /search [mobile], then the screenshot (and the mobile if not given)."""
    sess = order_search.start(message.chat.id, message.from_user.id, (command.args or "").strip() or None)
    await message.answer(order_search.intro_card(sess), parse_mode="HTML")


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject):
    """/add <mobile | registration | ORDER-ID>: add missing evidence to a case already sent to Betix."""
    q = (command.args or "").strip()
    if not q:
        await message.answer(
            para(
                "➕ Tell me which case to add to — an order id or the registered mobile number both work.",
                f"📱 {code('/add 9175404354')}\n🧾 {code('/add ILLUN-178921693146901')}",
                i("A registration number or Betix order number works too."),
            ),
            parse_mode="HTML",
        )
        return
    async with session_scope() as session:
        case = await add_evidence.find_case(session, q)
        if case is None:
            add_evidence.finish(message.chat.id)
            await message.answer("❌ " + b("Case not found."), parse_mode="HTML")
            return
        ev = await list_evidence(session, case.case_id)
        have = {e.type for e in ev}
        add_evidence.start(message.chat.id, message.from_user.id, case, q)
        pending = [t.replace("_", " ") for t in ("bank_statement", "payment_video") if t not in have]
        where = "already with Betix" if case.betix_root_message_id else "not with Betix yet"
        need = f"Still missing the {join_and(pending)}." if pending else "It already has everything."
        await message.answer(
            para(
                f"🗂 Found case {code(case_label(case))} for mobile {code(case.mobile or '-')} — {where}.",
                ("⏳ " if pending else "✅ ") + need + " 📎 Send the file now and I'll add it to this case.",
            ),
            parse_mode="HTML",
        )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if add_evidence.finish(message.chat.id) is not None:
        await message.answer("🗑 Stopped adding evidence.", parse_mode="HTML")
        return
    if order_search.cancel(message.chat.id):
        await message.answer("🗑 Search cancelled.", parse_mode="HTML")
        return
    async with session_scope() as session:
        case = await latest_case_for_user(session, message.chat.id, message.from_user.id)
        if case is not None:
            # Lock the row first: if the case is being processed right now, wait for that to finish and judge the
            # status it ends in - never discard a case that has just matched or been confirmed.
            await session.refresh(case, with_for_update=True)
        if not case or case.status != CaseStatus.WAITING_FOR_INPUT.value:
            await message.answer("ℹ️ There's nothing to cancel right now.", parse_mode="HTML")
            return
        await manager.fail_case(session, case, "cancelled by operator", actor=f"@{message.from_user.username}")
        await message.answer(f"🗑 Discarded case {code(case_label(case))}.", parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message, command: CommandObject):
    async with session_scope() as session:
        if command.args:
            q = command.args.strip()
            case = await get_case(session, q) or await find_case_by_order_id(session, q)
        else:
            case = await latest_case_for_user(session, message.chat.id, message.from_user.id)
        if not case:
            await message.answer("🤔 I couldn't find that case.", parse_mode="HTML")
            return
        text = await manager.summary_lines(session, case)
        ev = await list_evidence(session, case.case_id)
        missing = missing_items(case, ev)
    if missing and case.status == CaseStatus.WAITING_FOR_INPUT.value:
        text += "\n\n" + "\n".join(case_status_lines(case, ev))
    await message.answer(text, parse_mode="HTML")


@router.message(Command("cases"))
async def cmd_cases(message: Message):
    async with session_scope() as session:
        cases = await list_open_cases(session)
        if not cases:
            await message.answer("📭 No open cases right now. 🎉", parse_mode="HTML")
            return
        lines = [
            f"{STATUS_ICON.get(c.status, '▫️')} {code(case_label(c))} — "
            f"{esc(c.status.replace('_', ' ').lower())} · 📱 {esc(c.mobile or 'no mobile')}"
            for c in cases[-30:]
        ]
    await message.answer(para(f"🗂 {b(f'Open cases ({len(cases)})')}", "\n".join(lines)), parse_mode="HTML")


@router.message(Command("summary"))
async def cmd_summary(message: Message, command: CommandObject):
    """Overview card: ALL cases by stage (or /summary N for the last N days) and the open ones."""
    from app.telegram.summary import build_summary

    try:
        days = max(0, min(int((command.args or "0").strip()), 365))
    except ValueError:
        days = 0
    async with session_scope() as session:
        text = await build_summary(session, days)
    await message.answer(text, parse_mode="HTML")


def _forward_origin(message: Message) -> ForwardOrigin | None:
    """Preserve the ORIGINAL sender of a forwarded message (Bot API 7+ forward_origin)."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        # Older field names, kept as a fallback.
        legacy = getattr(message, "forward_from", None)
        if legacy is None:
            return None
        return ForwardOrigin(
            user_id=legacy.id,
            username=legacy.username,
            first_name=legacy.first_name,
            last_name=legacy.last_name,
            forwarded_at=getattr(message, "forward_date", None),
        )
    t = getattr(origin, "type", "")
    date = getattr(origin, "date", None)
    if t == "user":
        u = origin.sender_user
        return ForwardOrigin(
            user_id=u.id, username=u.username, first_name=u.first_name, last_name=u.last_name, forwarded_at=date
        )
    if t == "hidden_user":
        return ForwardOrigin(hidden_name=origin.sender_user_name, forwarded_at=date)
    if t == "chat":
        c = origin.sender_chat
        return ForwardOrigin(
            chat_id=c.id, username=getattr(c, "username", None), first_name=getattr(c, "title", None), forwarded_at=date
        )
    if t == "channel":
        c = origin.chat
        return ForwardOrigin(
            chat_id=c.id,
            message_id=origin.message_id,
            username=getattr(c, "username", None),
            first_name=getattr(c, "title", None),
            forwarded_at=date,
        )
    return ForwardOrigin(forwarded_at=date)


def _incoming_from_message(message: Message) -> IncomingInput:
    kind, file_id, uniq, mime, fname, size = "text", None, None, None, None, None
    if message.photo:
        p = message.photo[-1]
        kind, file_id, uniq, mime, size = "photo", p.file_id, p.file_unique_id, "image/jpeg", p.file_size
    elif message.video:
        v = message.video
        kind, file_id, uniq, mime, fname, size = (
            "video",
            v.file_id,
            v.file_unique_id,
            v.mime_type or "video/mp4",
            v.file_name,
            v.file_size,
        )
    elif message.document:
        d = message.document
        kind, file_id, uniq, mime, fname, size = (
            "document",
            d.file_id,
            d.file_unique_id,
            d.mime_type,
            d.file_name,
            d.file_size,
        )
    elif message.video_note:
        v = message.video_note
        kind, file_id, uniq, mime, size = "video", v.file_id, v.file_unique_id, "video/mp4", v.file_size
    return IncomingInput(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        kind=kind,
        text=message.text or message.caption,
        file_id=file_id,
        file_unique_id=uniq,
        mime_type=mime,
        filename=fname,
        size=size,
        forward=_forward_origin(message),
        raw={"date": message.date.isoformat() if message.date else None},
    )


async def track_progress(case_id: str, chat_id: int, sent) -> None:
    """Remember the message that will be edited live as the case moves along."""
    mid = getattr(sent, "message_id", None)
    if not mid:
        return
    async with session_scope() as session:
        case = await get_case_for_update(session, case_id)
        if case is not None:
            case.progress_chat_id, case.progress_message_id = chat_id, mid


_chat_locks: dict[int, asyncio.Lock] = {}


def _chat_lock(chat_id: int) -> asyncio.Lock:
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = _chat_locks[chat_id] = asyncio.Lock()
    return lock


async def _download_now(ev) -> None:
    try:
        from app.evidence.manager import download_evidence

        await download_evidence(notifications.get_bot(), ev)
    except Exception as exc:  # noqa: BLE001
        log.warning("early download failed", case_id=ev.case_id, error=str(exc))


async def sniff_unlabelled(inp: IncomingInput) -> str | None:
    """A document whose extension and MIME type say nothing (e.g. a forwarded MobiKwik statement arriving as
    "MobiKwik Txn Statement 11_Sept_2026-11_Sept_2026", application/octet-stream): read its first bytes and set the
    MIME type it really has, so it is classified like any other statement / screenshot / video."""
    from app.evidence.manager import SNIFF_MIME, classify_media, peek_document, sniff_type

    s = get_settings()
    if inp.kind != "document" or not inp.file_id:
        return None
    if classify_media(inp.kind, inp.mime_type, inp.filename) != "other":
        return None
    if inp.size and inp.size > s.evidence_max_download_mb * 1024 * 1024:
        return None
    try:
        found = sniff_type(await peek_document(inp.file_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read document to identify it", filename=inp.filename, error=str(exc)[:160])
        return None
    if found:
        log.info("document type from content", filename=inp.filename, was=inp.mime_type, type=found)
        inp.raw = {**(inp.raw or {}), "sniffed_type": found, "original_mime": inp.mime_type}
        inp.mime_type = SNIFF_MIME[found]
    return found


async def ingest(inp: IncomingInput) -> dict[str, Any]:
    """Attach one incoming message to its case (serialised per chat, case-id collisions retried)."""
    s = get_settings()
    await sniff_unlabelled(inp)
    async with _chat_lock(inp.chat_id):
        for attempt in range(4):
            try:
                async with session_scope() as session:
                    res = await attach_message(session, inp)
                    case = res.case
                    if res.evidence is not None and res.evidence.type == "bank_statement":
                        await _download_now(res.evidence)  # so a password-protected PDF is noticed immediately
                    if res.duplicate:
                        return {
                            "case": case,
                            "created": False,
                            "duplicate": True,
                            "evidence": [],
                            "missing": [],
                            "late_post": False,
                        }
                    ev = await list_evidence(session, case.case_id)
                    missing = missing_items(case, ev)
                    late_pending = bool(case.betix_root_message_id) and any(
                        e.type in manager.EXTRA_EVIDENCE_TYPES and not e.posted_to_betix_message_id for e in ev
                    )
                    allowed = s.betix_extra_evidence_policy == "always" or (
                        s.betix_extra_evidence_policy == "on_request"
                        and await case_evidence_requested(session, case.case_id)
                    )
                    # a password that arrives after the PDF went out is forwarded too (same policy)
                    late_post = allowed and (
                        late_pending or bool(case.betix_root_message_id and case.statement_password)
                    )
                    return {
                        "case": case,
                        "created": res.created,
                        "duplicate": False,
                        "evidence": ev,
                        "missing": missing,
                        "late_post": late_post,
                    }
            except IntegrityError:
                log.warning("case id collision, retrying", attempt=attempt + 1, chat_id=inp.chat_id)
                await asyncio.sleep(0.05 * (attempt + 1))
        raise RuntimeError("could not allocate a case id after 4 attempts")


def _customer_line(case, forwarded: bool) -> str:
    who = (
        case.original_username
        and f"@{case.original_username}"
        or " ".join(x for x in (case.original_first_name, case.original_last_name) if x)
        or "the sender"
    )
    return kv("👤", "Customer", who + (" (forwarded)" if forwarded else ""))


def case_card(case, evidence, missing, *, created: bool, forwarded: bool) -> str:
    """The one message per case in the operator chat: who it is for, what arrived, what happens next."""
    who = (
        case.original_username
        and f"@{case.original_username}"
        or " ".join(x for x in (case.original_first_name, case.original_last_name) if x)
        or "this customer"
    )
    what = "New Withdrawal" if case.kind == KIND_WITHDRAWAL else "New Case"
    icon = "\U0001f4b8" if case.kind == KIND_WITHDRAWAL else "\U0001f195"
    head = (
        f"{icon} {b(what)} \u2014 {esc(who)}"
        if created
        else f"\U0001f4ce {b('Evidence Added')} \u2014 {code(case_label(case))}"
    ) + (" (Forwarded)" if forwarded and created else "")
    if case.kind == KIND_WITHDRAWAL and not missing:
        nxt = "\U0001f4e4 Sending the withdrawal to Betix now."
    elif force_send_pending(case, missing):
        secs = get_settings().force_send_seconds
        nxt = (
            f"\u23f1 Waiting {secs}s for the {join_and(missing)}.\n"
            "\U0001f4e4 After that I'll send the case to Betix with what I have."
        )
    elif missing:
        nxt = "\U0001f4e9 Waiting for the " + join_and(missing) + "."
    else:
        nxt = "\U0001f50e All evidence received.\n\u23f3 Checking the order in Illunise..."
    return para(head, case_status_lines(case, evidence), nxt)


# ---- THE 5-SECOND COLLECTION BUFFER ------------------------------------------------------------------------
# Nothing happens when a message arrives: it is held. COLLECTION_SECONDS after the FIRST held message of a chat,
# everything held is attached in order (ONE case for the batch), ONE card is sent, and the case is checked once:
# all four items in -> processed; something missing -> the card says what and the case waits.
_held: dict[int, list[tuple[Message, IncomingInput]]] = {}
_flush_tasks: dict[int, asyncio.Task] = {}


def hold(message: Message, inp: IncomingInput) -> bool:
    """Queue a message for its chat. Returns True when this message opened the chat's window."""
    chat_id = inp.chat_id
    first = chat_id not in _held
    _held.setdefault(chat_id, []).append((message, inp))
    if first or chat_id not in _flush_tasks or _flush_tasks[chat_id].done():
        _flush_tasks[chat_id] = asyncio.create_task(_flush_later(chat_id, get_settings().collection_seconds))
        return True
    return False


async def _flush_later(chat_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await flush_chat(chat_id)
    except Exception:  # noqa: BLE001
        log.exception("collection flush failed", chat_id=chat_id)


def force_send_pending(case, missing: list[str]) -> bool:
    """Only the statement / video (/ its password) is missing and the FORCE SEND wait is on."""
    return (
        bool(missing)
        and case.kind != KIND_WITHDRAWAL  # a withdrawal is never sent without its statement
        and not hard_missing(missing)
        and get_settings().force_send_seconds > 0
        and case.status == CaseStatus.WAITING_FOR_INPUT.value
    )


async def flush_chat(chat_id: int) -> list[str]:
    """Attach everything held for the chat, send ONE card per case touched, start processing where complete."""
    s = get_settings()
    items = _held.pop(chat_id, [])
    # The payment screenshot opens the case, so it goes first whatever order the batch arrived in; everything
    # else keeps its arrival order (a PDF before its password, the mobile wherever it was typed). A statement
    # handled before its own screenshot would go looking for an older case to join.
    items.sort(key=lambda mi: 0 if mi[1].kind == "photo" else 1)
    touched: dict[str, dict[str, Any]] = {}
    for message, inp in items:
        r = await ingest(inp)
        if r["duplicate"]:
            continue
        case = r["case"]
        entry = touched.setdefault(
            case.case_id, {"created": False, "forwarded": False, "late_post": False, "message": message}
        )
        entry.update(case=case, evidence=r["evidence"], missing=r["missing"], message=message)
        entry["created"] = entry["created"] or r["created"]
        entry["forwarded"] = entry["forwarded"] or inp.forward is not None
        entry["late_post"] = entry["late_post"] or r["late_post"]
    for case_id, e in touched.items():
        case = e["case"]
        if case.betix_root_message_id:
            # Late statement / video for a case already in Betix: no new message, the live card is refreshed.
            if e["late_post"]:
                await enqueue("late_evidence_job", case_id, job_id=f"late-{case_id}-{e['message'].message_id}")
            await refresh_progress(case_id)
            continue
        if not e["missing"] and case.status == CaseStatus.WAITING_FOR_INPUT.value:
            async with session_scope() as session:
                c = await get_case_for_update(session, case_id)
                c.processing_version += 1
                version = c.processing_version
            await enqueue("process_case_job", case_id, version, True, job_id=f"process-{case_id}-{version}")
        elif force_send_pending(case, e["missing"]):
            # Screenshot + mobile are in; only the statement / video is missing. (Re)start the FORCE SEND wait: a
            # newer version makes any earlier timer stale, so every new file restarts the 30 seconds.
            async with session_scope() as session:
                c = await get_case_for_update(session, case_id)
                c.processing_version += 1
                version = c.processing_version
            await enqueue(
                "process_case_job",
                case_id,
                version,
                True,
                True,
                job_id=f"force-{case_id}-{version}",
                defer_seconds=s.force_send_seconds,
            )
        text = case_card(case, e["evidence"], e["missing"], created=e["created"], forwarded=e["forwarded"])
        # ONE message per case: created once, edited afterwards (and by every processing step).
        if not e["created"] and case.progress_chat_id == chat_id and case.progress_message_id:
            if await edit_case_message(case.progress_chat_id, case.progress_message_id, text):
                continue
        sent = await e["message"].answer(text, parse_mode="HTML")
        await track_progress(case_id, chat_id, sent)
    return list(touched)


@router.message(F.chat.type == "private", F.photo | F.video | F.document | F.video_note | F.text)
async def on_evidence(message: Message):
    inp = _incoming_from_message(message)
    if inp.kind == "text" and (inp.text or "").startswith("/"):
        return
    adding = add_evidence.active(message.chat.id)
    if adding is not None and inp.file_id:
        if add_evidence.belongs_elsewhere(adding, inp):
            # another customer's forward: the /add is over, this file opens / joins a case of its own
            add_evidence.finish(message.chat.id)
            log.info("/add closed: a forward from another customer arrived", case_id=adding.case_id)
            await message.answer(
                para(
                    f"↩️ Closed {code('/add')} for {code(adding.case_id)} — this file is from a different customer, "
                    "so it is handled as its own case."
                ),
                parse_mode="HTML",
            )
        else:
            await add_to_case(message, inp, adding)
            return
    sess = order_search.active(message.chat.id)
    if sess is not None:
        # /search mode: this message feeds the lookup, never a case.
        order_search.take(sess, inp)
        lines = order_search.status_lines(sess)
        if order_search.missing(sess):
            await message.answer(para(f"🔎 {b('Order search')}", lines), parse_mode="HTML")
            return
        await message.answer(para(f"🔎 {b('Order search')}", lines, "⏳ Searching Illunise orders…"), parse_mode="HTML")
        try:
            reply = await order_search.run(sess)
        finally:
            order_search.finish(message.chat.id)
        await message.answer(reply, parse_mode="HTML")
        return
    hold(message, inp)  # silence for COLLECTION_SECONDS, then ONE case, ONE card, ONE check


async def add_to_case(message: Message, inp: IncomingInput, sess) -> None:
    """A file sent after /add: attach it to THAT case and post it as a reply to its Betix screenshot."""
    async with session_scope() as session:
        case = await get_case_for_update(session, sess.case_id)
        if case is None:
            add_evidence.finish(message.chat.id)
            await message.answer("❌ " + b("Case not found."), parse_mode="HTML")
            return
        ev, note = await add_evidence.attach(session, case, inp)
        posted = bool(case.betix_root_message_id)
        label, case_id = case_label(case), case.case_id
        done = ev is not None and add_evidence.complete(case, await list_evidence(session, case_id))
    if ev is None:
        await message.answer(f"↩️ Not added to {code(label)} — {esc(note)}.", parse_mode="HTML")
        return
    sess.added.append(note)
    if posted:
        await enqueue("added_evidence_job", case_id, job_id=f"added-{case_id}-{inp.message_id}")
        tail = "📤 Sending it to Betix now, as a reply to the original screenshot."
    else:
        tail = "📩 It will go out with the case."
    if done:
        add_evidence.finish(message.chat.id)  # everything is on the case: the /add closes itself
        tail += f"\n✅ The case has everything now — {code('/add')} closed."
    await message.answer(para(f"✅ Added the {esc(note)} to case {code(label)}.", tail), parse_mode="HTML")


async def edit_case_message(chat_id: int, message_id: int, text: str) -> bool:
    try:
        await get_throttle().run(
            chat_id,
            lambda: notifications.get_bot().edit_message_text(
                text, chat_id=chat_id, message_id=message_id, parse_mode="HTML"
            ),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        if "not modified" in str(exc):
            return True
        log.warning("case message edit failed; sending a new one", error=str(exc)[:160])
        return False


async def refresh_progress(case_id: str) -> None:
    from app.telegram.progress import push

    async with session_scope() as session:
        case = await get_case(session, case_id)
        if case is not None:
            await push(session, case)


STATUS_ICON = {
    "WAITING_FOR_INPUT": "⏳",
    "ANALYZING_EVIDENCE": "🧠",
    "SEARCHING_ORDER": "🔎",
    "ORDER_MATCH_FOUND": "🎯",
    "ORDER_MATCH_AMBIGUOUS": "⚠️",
    "CHECKING_ORDER_UPI": "🏦",
    "READY_FOR_BETIX": "📤",
    "POSTED_TO_BETIX": "📨",
    "WAITING_FOR_CONFIRMATION": "👀",
    "FOLLOWUP_1_SENT": "🔔",
    "FOLLOWUP_2_SENT": "🔔🔔",
    "VERIFIED": "✅",
    "ESCALATED": "🚨",
    "FAILED": "⛔",
    "ALREADY_SUCCESS": "♻️",
    "ALREADY_SENT": "🔁",
}


def evidence_names(evidence) -> list[str]:
    """What is on the case, in plain words."""
    names = {
        "payment_screenshot": "payment screenshot",
        "bank_statement": "bank statement",
        "payment_video": "payment video",
    }
    types = {e.type for e in evidence}
    return [label for t, label in names.items() if t in types]


def case_status_lines(case, evidence) -> list[str]:
    """One short line per item so the operator can scan the case at a glance: ticked when it has arrived."""
    types = {e.type for e in evidence}
    if case.kind == KIND_WITHDRAWAL:
        lines = [
            (
                f"\u2705 Withdrawal ID: {code(case.withdrawal_id)}"
                if case.withdrawal_id
                else "\u23f3 Withdrawal ID \u2014 waiting"
            ),
            ("\u2705 Bank Statement" if "bank_statement" in types else "\u23f3 Bank Statement \u2014 waiting"),
        ]
        if statement_needs_password(case, evidence):
            lines.append("\U0001f510 Statement is password protected \u2014 please send the password.")
        return lines
    lines = [
        ("\u2705 Payment Screenshot" if "payment_screenshot" in types else "\u23f3 Payment Screenshot \u2014 waiting"),
        (f"\u2705 Mobile: {code(case.mobile)}" if case.mobile else "\u23f3 Mobile \u2014 waiting"),
        ("\u2705 Bank Statement" if "bank_statement" in types else "\u23f3 Bank Statement \u2014 waiting"),
        ("\u2705 Payment Video" if "payment_video" in types else "\u23f3 Payment Video \u2014 waiting"),
    ]
    if statement_needs_password(case, evidence):
        lines.append("\U0001f510 Statement is password protected \u2014 please send the password.")
    return lines


# The "/" menu in Telegram. Registered with the Bot API on EVERY start so the menu always matches the code.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("add", "Add missing evidence to a case already sent to Betix (/add MOBILE)"),
    ("search", "Find the Illunise order id for a screenshot (nothing posted)"),
    ("status", "Where the current case stands (or /status ORDER-ID)"),
    ("cases", "All open cases"),
    ("summary", "All-time overview by stage (or /summary 7 for a week)"),
    ("cancel", "Discard the case (or search) in progress"),
    ("restart", "Pull the latest code and restart the bot"),
    ("help", "How to use the bot"),
]


async def register_commands(bot: Bot) -> int:
    """Publish the command menu for PRIVATE chats only and make sure groups (the Betix group) show no menu at
    all. Replaces whatever Telegram had. Best-effort: a failure is logged, not fatal."""
    cmds = [BotCommand(command=c, description=d[:256]) for c, d in BOT_COMMANDS]
    try:
        await bot.set_my_commands(cmds, scope=BotCommandScopeAllPrivateChats())
        for scope in (BotCommandScopeDefault(), BotCommandScopeAllGroupChats(), BotCommandScopeAllChatAdministrators()):
            await bot.delete_my_commands(scope=scope)  # no "/" menu in groups
        log.info("bot commands registered", count=len(cmds), scope="private chats only")
        return len(cmds)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not register bot commands", error=str(exc)[:200])
        return 0


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    if get_settings().betix_monitor_mode == "bot":
        dp.include_router(build_betix_router())
    return dp


async def run_polling(stop_event: asyncio.Event | None = None) -> None:
    bot = notifications.get_bot()
    dp = build_dispatcher()
    me = await bot.get_me()
    from app.telegram.betix_monitor import register_self

    register_self(me.id, me.username)  # our own messages in the Betix group are never a confirmation
    await register_commands(bot)  # the "/" menu, refreshed on every run
    log.info("input bot started", username=me.username)
    from app import restart

    asked_by = restart.pop_notice()
    if asked_by:
        try:
            await bot.send_message(
                asked_by, para("\u2705 " + b("BOT RESTARTED"), "\U0001f7e2 Running again."), parse_mode="HTML"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not confirm the restart", error=str(exc)[:200])
    if stop_event is None:
        await dp.start_polling(bot, allowed_updates=["message"])
    else:
        task = asyncio.create_task(dp.start_polling(bot, allowed_updates=["message"], handle_signals=False))
        await stop_event.wait()
        await dp.stop_polling()
        await task
