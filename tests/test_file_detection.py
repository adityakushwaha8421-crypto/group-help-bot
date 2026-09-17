"""Evidence type comes from the file extension + Telegram MIME type only (no AI, no caption guessing),
and a password-protected statement PDF is still a bank statement: the bot asks for the password."""

from pathlib import Path

import pytest

from app.cases.correlation import attach_message, missing_items, statement_needs_password
from app.db.repository import list_evidence
from app.evidence.manager import classify_media
from app.evidence.pdf import decrypt_pdf, pdf_is_encrypted
from app.telegram.input_bot import case_status_lines
from tests.conftest import make_input

MOB = "7733931348"


@pytest.mark.parametrize(
    "kind,mime,filename,expected",
    [
        # the user's examples
        ("document", "application/pdf", "statement.pdf", "bank_statement"),
        ("document", "video/mp4", "payment.mp4", "payment_video"),
        ("document", "image/jpeg", "payment.jpg", "payment_screenshot"),
        # extension alone
        ("document", None, "x.PDF", "bank_statement"),
        # MobiKwik saves its statement PDF as "Txn Statement ....uu"
        ("document", "application/octet-stream", "MobiKwik Txn Statement 27_Aug_2026-27_Aug_2026.uu", "bank_statement"),
        ("document", None, "statement.UU", "bank_statement"),
        ("document", "text/x-uuencode", None, "bank_statement"),
        ("document", None, "clip.mov", "payment_video"),
        ("document", None, "clip.mkv", "payment_video"),
        ("document", None, "clip.avi", "payment_video"),
        ("document", None, "clip.webm", "payment_video"),
        ("document", None, "clip.m4v", "payment_video"),
        ("document", None, "clip.3gp", "payment_video"),
        ("document", None, "shot.jpeg", "payment_screenshot"),
        ("document", None, "shot.png", "payment_screenshot"),
        ("document", None, "shot.webp", "payment_screenshot"),
        # Telegram MIME alone (file sent without a useful name)
        ("document", "application/pdf", None, "bank_statement"),
        ("document", "video/quicktime", None, "payment_video"),
        ("document", "video/x-msvideo", None, "payment_video"),
        ("document", "image/png", None, "payment_screenshot"),
        ("document", "image/heic", None, "payment_screenshot"),
        # Telegram message kinds
        ("photo", "image/jpeg", None, "payment_screenshot"),
        ("video", "video/mp4", None, "payment_video"),
        ("video_note", "video/mp4", None, "payment_video"),
        # unknown stays unknown; a caption never changes the type
        ("document", "application/zip", "archive.zip", "other"),
        ("document", "text/plain", "notes.txt", "other"),
    ],
)
def test_type_from_extension_and_mime(kind, mime, filename, expected):
    assert classify_media(kind, mime, filename, "bank statement attached") == expected


def make_pdf(path: Path, password: str | None = None) -> Path:
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    if password:
        w.encrypt(password)
    with path.open("wb") as fh:
        w.write(fh)
    return path


def test_pdf_password_detection_and_decrypt(tmp_path):
    plain = make_pdf(tmp_path / "plain.pdf")
    locked = make_pdf(tmp_path / "locked.pdf", "1234")
    assert pdf_is_encrypted(plain) is False and pdf_is_encrypted(locked) is True
    assert pdf_is_encrypted(tmp_path / "missing.pdf") is False
    assert decrypt_pdf(locked, "wrong") is None
    out = decrypt_pdf(locked, "1234")
    assert out is not None and out.exists() and pdf_is_encrypted(out) is False


async def test_protected_statement_is_still_a_statement_and_password_is_requested(db, tmp_path):
    locked = make_pdf(tmp_path / "statement.pdf", "4321")
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        await attach_message(s, make_input(2, "text", MOB))
        st = await attach_message(s, make_input(3, "document", filename="statement.pdf"))
        await attach_message(s, make_input(4, "video"))
        st.evidence.local_path, st.evidence.downloaded = str(locked), True  # what the early download does
        case, ev = r.case, await list_evidence(s, r.case.case_id)
        assert [e.type for e in ev if e.telegram_message_id == 3] == ["bank_statement"]
        assert statement_needs_password(case, ev)
        assert missing_items(case, ev) == ["statement password"]
        lines = case_status_lines(case, ev)
        assert lines[-1] == "🔐 Statement is password protected — please send the password."
        # the admin replies with the bare password -> accepted, nothing pending
        await attach_message(s, make_input(5, "text", "4321"))
        ev = await list_evidence(s, case.case_id)
        assert case.statement_password == "4321" and missing_items(case, ev) == []
        assert not any("waiting" in l or "password" in l for l in case_status_lines(case, ev))


async def test_password_caption_and_prefixed_password_still_work(db, tmp_path):
    locked = make_pdf(tmp_path / "statement.pdf", "lata2812")
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        st = await attach_message(s, make_input(3, "document", "Password:- lata2812", filename="statement.pdf"))
        st.evidence.local_path = str(locked)
        ev = await list_evidence(s, r.case.case_id)
        assert r.case.statement_password == "lata2812" and not statement_needs_password(r.case, ev)


async def test_chatter_is_not_taken_as_a_password(db, tmp_path):
    locked = make_pdf(tmp_path / "statement.pdf", "9999")
    async with db.session_scope() as s:
        r = await attach_message(s, make_input(1, "photo"))
        st = await attach_message(s, make_input(3, "document", filename="statement.pdf"))
        st.evidence.local_path = str(locked)
        for word in ("ok", "done", "please check", "thanks"):
            await attach_message(s, make_input(10 + len(word), "text", word))
        assert r.case.statement_password is None


# ---------------------------------------------------------------- .uu statements
def test_uu_statement_is_read_as_the_pdf_it_is(tmp_path):
    from app.evidence.pdf import pdf_view

    uu = make_pdf(tmp_path / "x.pdf").rename(tmp_path / "MobiKwik Txn Statement.uu")
    view = pdf_view(uu)
    assert view is not None and view.suffix == ".pdf" and view.read_bytes() == uu.read_bytes()
    assert pdf_view(tmp_path / "missing.uu") is None
    junk = tmp_path / "junk.uu"
    junk.write_bytes(b"not a statement")
    assert pdf_view(junk) is None


def test_real_uuencoded_pdf_is_decoded(tmp_path):
    import binascii

    from app.evidence.pdf import pdf_view

    raw = make_pdf(tmp_path / "s.pdf").read_bytes()
    body = b"".join(binascii.b2a_uu(raw[i : i + 45]) for i in range(0, len(raw), 45))
    uu = tmp_path / "statement.uu"
    uu.write_bytes(b"begin 644 statement.pdf\n" + body + b"`\nend\n")
    assert pdf_view(uu).read_bytes() == raw


def test_password_protected_uu_statement_asks_for_the_password(tmp_path):
    from app.cases.correlation import statement_needs_password  # noqa: F401  (content based: works for .uu)
    from app.evidence.pdf import pdf_is_encrypted, pdf_view

    uu = make_pdf(tmp_path / "l.pdf", "4321").rename(tmp_path / "locked.uu")
    assert pdf_is_encrypted(uu) and pdf_is_encrypted(pdf_view(uu))


async def test_uu_statement_goes_to_the_ai_as_a_pdf(db, tmp_path, fake_ai):
    from app.db.models import Evidence
    from app.evidence.manager import analyze_evidence

    seen = []
    orig = fake_ai.analyze_files

    async def spy(files, *, doc_type_hint, context_hint=""):
        seen.extend(files)
        return await orig(files, doc_type_hint=doc_type_hint, context_hint=context_hint)

    fake_ai.analyze_files = spy
    uu = make_pdf(tmp_path / "x.pdf").rename(tmp_path / "MobiKwik Txn Statement.uu")
    ev = Evidence(case_id="C", telegram_chat_id=1, telegram_message_id=1, type="bank_statement", local_path=str(uu))
    async with db.session_scope() as s:
        await analyze_evidence(s, ev)
    assert [f.suffix for f in seen] == [".pdf"]


# ---------------------------------------------------------------- unlabelled documents: type from content
MOBIKWIK = "MobiKwik Txn Statement 11_Sept_2026-11_Sept_2026"  # as Telegram delivered it: no extension


@pytest.mark.parametrize(
    "data,expected",
    [
        (b"%PDF-1.4\n%\xf6\xe4", "bank_statement"),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "payment_screenshot"),
        (b"\x89PNG\r\n\x1a\n\x00", "payment_screenshot"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8", "payment_screenshot"),
        (b"\x00\x00\x00\x18ftypheic\x00", "payment_screenshot"),
        (b"\x00\x00\x00\x18ftypmp42\x00", "payment_video"),
        (b"\x00\x00\x00\x14ftypqt  \x00", "payment_video"),
        (b"\x1a\x45\xdf\xa3\x9f", "payment_video"),
        (b"PK\x03\x04zip", None),
        (b"", None),
    ],
)
def test_sniff_type(data, expected):
    from app.evidence.manager import sniff_type

    assert sniff_type(data) == expected


@pytest.fixture
def peek(tmp_path):
    from app.evidence import manager as evm

    served = {}

    async def fake_peek(file_id):
        return served.get(file_id)

    evm.set_peeker(fake_peek)
    yield served
    evm.set_peeker(None)


async def test_extensionless_octet_stream_pdf_is_a_bank_statement(db, fake_bot, no_download, peek, tmp_path):
    """Live 2026-09-11 (CASE-20260911-000013): the forwarded MobiKwik statement came in as
    "MobiKwik Txn Statement 11_Sept_2026-11_Sept_2026", application/octet-stream, and was filed as "other"."""
    from app.telegram import input_bot

    peek["doc3"] = make_pdf(tmp_path / "s.pdf").read_bytes()
    await input_bot.ingest(make_input(1, "photo"))
    await input_bot.ingest(make_input(2, "text", MOB))
    doc = make_input(3, "document", filename=MOBIKWIK, mime_type="application/octet-stream")
    r = await input_bot.ingest(doc)
    await input_bot.ingest(make_input(4, "video"))
    async with db.session_scope() as s:
        ev = await list_evidence(s, r["case"].case_id)
        st = [e for e in ev if e.telegram_message_id == 3]
        assert [e.type for e in st] == ["bank_statement"] and st[0].mime_type == "application/pdf"
        assert missing_items(r["case"], ev) == []


async def test_unknown_content_stays_other(db, fake_bot, no_download, peek):
    from app.telegram import input_bot

    peek["doc3"] = b"PK\x03\x04 a zip"
    r = await input_bot.ingest(make_input(3, "document", filename="archive", mime_type="application/octet-stream"))
    async with db.session_scope() as s:
        assert [e.type for e in await list_evidence(s, r["case"].case_id)] == ["other"]


async def test_labelled_files_are_never_downloaded_to_sniff(db, fake_bot, no_download, peek):
    from app.telegram import input_bot

    calls = []

    async def spy(file_id):
        calls.append(file_id)
        return None

    from app.evidence import manager as evm

    evm.set_peeker(spy)
    await input_bot.ingest(make_input(1, "photo"))
    await input_bot.ingest(make_input(3, "document"))  # PhonePe_Statement.pdf
    assert calls == []
