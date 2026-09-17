"""Evidence manager: classify incoming Telegram media, download it, run AI extraction per file."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.analyzer import AIUnavailable, get_analyzer
from app.ai.extractor import Extraction
from app.config import get_settings
from app.db.models import Evidence, EvidenceType
from app.db.repository import audit
from app.evidence.files import retention_expiry, target_path
from app.evidence.ocr import extract_keyframes
from app.evidence.pdf import decrypt_pdf, pdf_is_encrypted, pdf_view
from app.utils.logging import get_logger

log = get_logger("evidence")

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
VIDEO_EXT = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".3gp")
# Bank statements: PDFs, and ".uu" (MobiKwik saves its "Txn Statement" PDF under that extension).
PDF_EXT = (".pdf", ".uu")
STATEMENT_MIME = ("application/pdf", "text/x-uuencode")


def classify_media(kind: str, mime: str | None, filename: str | None, caption: str | None = None) -> str:  # noqa: ARG001
    """Evidence type from the file EXTENSION and the Telegram MIME type only. Deterministic, no AI, no caption
    guessing:  .pdf / .uu / application/pdf -> bank statement;  video ext / video/* -> payment video;
    image ext / image/* -> payment screenshot.  A password-protected PDF is still a bank statement."""
    fn = (filename or "").lower()
    mime = (mime or "").lower()
    if fn.endswith(PDF_EXT) or mime in STATEMENT_MIME:
        return EvidenceType.bank_statement.value
    if fn.endswith(VIDEO_EXT) or mime.startswith("video/") or kind in ("video", "video_note"):
        return EvidenceType.payment_video.value
    if fn.endswith(IMAGE_EXT) or mime.startswith("image/") or kind == "photo":
        return EvidenceType.payment_screenshot.value
    return EvidenceType.other.value


# What a document IS, from its first bytes (no AI): used only when neither the extension nor the Telegram MIME
# type says anything (forwarded files often arrive as "application/octet-stream" with no extension at all).
SNIFF_MIME = {
    EvidenceType.bank_statement.value: "application/pdf",
    EvidenceType.payment_screenshot.value: "image/jpeg",
    EvidenceType.payment_video.value: "video/mp4",
}


def sniff_type(data: bytes | None) -> str | None:
    """bank_statement for a PDF (or a uuencoded PDF), payment_screenshot for an image, payment_video for a video."""
    if not data:
        return None
    head = data[:64]
    if data[:1024].lstrip().startswith(b"%PDF"):
        return EvidenceType.bank_statement.value
    if data.lstrip().startswith(b"begin "):
        from app.evidence.pdf import _uudecode

        inner = _uudecode(data) or b""
        return EvidenceType.bank_statement.value if inner.lstrip().startswith(b"%PDF") else None
    if (
        head[:3] == b"\xff\xd8\xff"
        or head[:8] == b"\x89PNG\r\n\x1a\n"
        or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
    ):
        return EvidenceType.payment_screenshot.value
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"heim", b"heis", b"mif1", b"msf1", b"avif"):
            return EvidenceType.payment_screenshot.value
        return EvidenceType.payment_video.value
    if head[:4] == b"\x1a\x45\xdf\xa3" or (head[:4] == b"RIFF" and head[8:12] == b"AVI "):
        return EvidenceType.payment_video.value
    return None


_peeker = None


def set_peeker(fn) -> None:
    """Tests: replace the Telegram download used by peek_document (fn(file_id) -> bytes | None)."""
    global _peeker
    _peeker = fn


async def peek_document(file_id: str) -> bytes | None:
    """Fetch a (small) Telegram document's bytes to sniff its type."""
    if _peeker is not None:
        return await _peeker(file_id)
    import io

    from app.telegram.notifications import get_bot

    bot = get_bot()
    tg_file = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(tg_file.file_path, destination=buf)
    return buf.getvalue()


async def download_evidence(bot, ev: Evidence) -> Path | None:
    """Download a Telegram file via the Bot API (<= 20MB). Returns local path or None."""
    s = get_settings()
    if ev.local_path and Path(ev.local_path).exists():
        return Path(ev.local_path)
    if ev.size and ev.size > s.evidence_max_download_mb * 1024 * 1024:
        log.warning(
            "evidence too large for Bot API download; will forward by file_id", case_id=ev.case_id, size=ev.size
        )
        return None
    ext = ""
    if ev.type == EvidenceType.payment_video.value:
        ext = ".mp4"
    elif ev.mime_type == "application/pdf":
        ext = ".pdf"
    elif ev.type == EvidenceType.payment_screenshot.value:
        ext = ".jpg" if not (ev.filename or "").lower().endswith(".png") else ".png"
    path = target_path(ev.case_id, ev.telegram_message_id, ev.filename, ext)
    try:
        tg_file = await bot.get_file(ev.file_id)
        await bot.download_file(tg_file.file_path, destination=str(path))
    except Exception as exc:  # noqa: BLE001
        log.error("evidence download failed", case_id=ev.case_id, error=str(exc))
        return None
    ev.local_path = str(path)
    ev.downloaded = True
    ev.expires_at = retention_expiry()
    return path


async def analyze_evidence(
    session: AsyncSession, ev: Evidence, context_hint: str = "", statement_password: str | None = None
) -> Extraction:
    """Run GPT-5.6 Terra over one evidence file. Stores the raw payload on the evidence row.

    A file that was already read successfully is not sent to the model again: the case may be processed several
    times (a retry, a restart, a late statement) and every re-read used to cost a call and rate-limit headroom."""
    s = get_settings()
    stored = ev.analysis or {}
    if stored.get("extraction"):
        log.debug("evidence already analysed; reusing", case_id=ev.case_id, type=ev.type)
        payload = stored.get("payload")
        if isinstance(payload, dict) and payload:
            from app.ai.extractor import extraction_from_ai

            return extraction_from_ai(payload, source=ev.type)  # the model's raw answer, normalised by today's rules
        return Extraction.from_dict(stored["extraction"])
    if not ev.local_path or not Path(ev.local_path).exists():
        return Extraction()
    path = Path(ev.local_path)
    files: list[Path] = []
    if ev.type == EvidenceType.payment_video.value:
        if s.video_keyframes_enabled:
            files = await extract_keyframes(path, s.video_keyframe_count)
    elif ev.type == EvidenceType.bank_statement.value and pdf_is_encrypted(pdf_view(path) or path):
        # The model cannot open a protected PDF: analyse a decrypted copy, or skip until the password arrives.
        path = pdf_view(path) or path
        if not statement_password:
            ev.analysis = {"skipped": "password protected; no password on the case"}
            return Extraction()
        dec = decrypt_pdf(path, statement_password)
        if dec is None:
            ev.analysis = {"error": "statement password rejected"}
            await audit(session, "STATEMENT_PASSWORD_INVALID", case_id=ev.case_id, result="error", source=ev.type)
            return Extraction()
        files = [dec]
    elif ev.type == EvidenceType.bank_statement.value:
        files = [pdf_view(path) or path]  # a .uu statement is read as the PDF it is
    else:
        files = [path]
    if not files:
        ev.analysis = {"skipped": "no analyzable frames/files"}
        return Extraction()
    try:
        extraction, payload = await get_analyzer().analyze_files(
            files, doc_type_hint=ev.type, context_hint=context_hint
        )
    except AIUnavailable as exc:
        ev.analysis = {"error": str(exc), "unavailable": True}  # the reader never ran: nothing is known yet
        await audit(
            session,
            "AI_EXTRACTION_FAILED",
            case_id=ev.case_id,
            result="error",
            source=ev.type,
            details={"error": str(exc)},
        )
        return Extraction()
    except Exception as exc:  # noqa: BLE001
        ev.analysis = {"error": repr(exc)}
        await audit(
            session,
            "AI_EXTRACTION_FAILED",
            case_id=ev.case_id,
            result="error",
            source=ev.type,
            details={"error": repr(exc)},
        )
        return Extraction()
    ev.analysis = {"payload": payload, "extraction": extraction.as_dict()}
    await audit(
        session,
        "AI_EXTRACTION_COMPLETED",
        case_id=ev.case_id,
        result="ok",
        source=ev.type,
        confidence=max([extraction.amount.confidence, extraction.payment_time.confidence] or [0]),
        details={
            "fields": {
                k: getattr(extraction, k).as_dict()
                for k in Extraction.FIELDS
                if getattr(extraction, k).value is not None
            }
        },
    )
    return extraction
