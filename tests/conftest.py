"""Test fixtures: SQLite database, fake Telegram bot, fake Betix poster, fake AI analyzer, fake order search."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

TEST_ENV = {
    "APP_ENV": "test",
    "INLINE_JOBS": "true",
    "TELEGRAM_BOT_TOKEN": "",
    "ADMIN_TELEGRAM_USER_IDS": "111",
    "ADMIN_NOTIFY_CHAT_ID": "111",
    "TG_GROUP_SEND_INTERVAL_SECONDS": "0",  # no real spacing between fake sends
    "TG_PRIVATE_SEND_INTERVAL_SECONDS": "0",
    "BETIX_GROUP_CHAT_ID": "-1009999",
    "AUTHORIZED_BETIX_REVIEWER_IDS": "5001",
    "AUTHORIZED_BETIX_REVIEWER_USERNAMES": "wendy,queenie",
    "BETIX_SYSTEM_BOT_USERNAMES": "betixpay_cs_bot",
    "CONFIRMATION_MODE": "strict",
    "CASE_DEBOUNCE_SECONDS": "0",
    "CASE_COLLECTION_WINDOW_MINUTES": "30",
    "FOLLOWUP_SCHEDULE_MINUTES": "15,45",
    "ESCALATION_DELAY_MINUTES": "30",
    "OPENAI_API_KEY": "",
    "VIDEO_KEYFRAMES_ENABLED": "false",
    "AI_CLASSIFY_UNKNOWN_BETIX_REPLIES": "false",
    "ORDER_MATCH_THRESHOLD": "0.90",
    "PAYMENT_TIME_WINDOW_MINUTES": "15",
    "REQUIRE_ALL_EVIDENCE": "true",
    "BETIX_EXTRA_EVIDENCE_POLICY": "always",
    "COLLECTION_SECONDS": "5",
    "BATCH_JOIN_SECONDS": "15",
}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "evidence"))
    monkeypatch.setenv("ADMIN_STORAGE_STATE_PATH", str(tmp_path / "state.json"))
    from app.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest_asyncio.fixture
async def db(env):
    from app.db import session as dbs

    await dbs.dispose()
    await dbs.create_all()
    yield dbs
    await dbs.dispose()


class FakeBot:
    """Stands in for aiogram.Bot for notifications and evidence download."""

    def __init__(self):
        self.sent: list[tuple] = []
        self.edits: list[tuple] = []  # (chat_id, message_id, text) from edit_message_text
        self._n = 1000

    async def send_message(self, chat_id, text, **kw):
        self._n += 1
        self.sent.append((chat_id, text))
        return type("M", (), {"message_id": self._n})()

    async def edit_message_text(self, text, *, chat_id, message_id, **kw):
        self.edits.append((chat_id, message_id, text))
        return True

    async def get_file(self, file_id):
        return type("F", (), {"file_path": f"fake/{file_id}"})()

    async def download_file(self, file_path, destination):
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        Path(destination).write_bytes(b"\xff\xd8fake")


@pytest.fixture
def fake_bot(env):
    from app.telegram import notifications

    bot = FakeBot()
    notifications.set_bot(bot)
    yield bot
    notifications.set_bot(None)


class FakePoster:
    """Real BetixPoster logic with the Telegram send calls replaced by recorders."""

    def __init__(self):
        from app.config import get_settings
        from app.telegram.betix_poster import BetixPoster

        real = BetixPoster.__new__(BetixPoster)
        real.s = get_settings()
        real.default_chat = real.chat = -1009999
        self._real = real
        self.texts: list[tuple[str, int | None]] = []
        self.chats: list[int | str] = []  # which group each send went to, in order
        self.media: list[tuple[str, str | None]] = []
        self.media_replies: list[tuple[str, int | None]] = []  # (evidence type, reply_to message id)
        self._n = 500

        async def _send_text(text, reply_to=None):
            self._n += 1
            self.texts.append((text, reply_to))
            self.chats.append(real.chat)
            return self._n

        async def _send_media(ev, caption, reply_to=None):
            self._n += 1
            self.chats.append(real.chat)
            self.media.append((ev.type, caption))
            self.media_replies.append((ev.type, reply_to))
            return self._n

        self.deleted: list[int] = []
        self.undeletable: set[int] = set()  # e.g. another member's message while our bot is not an admin

        async def _delete(message_id):
            if message_id in self.undeletable:
                raise RuntimeError("Bad Request: message can't be deleted for everyone")
            self.deleted.append(message_id)

        real._send_text = _send_text
        real._send_media = _send_media
        real._delete = _delete

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def fake_poster(env):
    return FakePoster()


class FakeAnalyzer:
    def __init__(self, screenshot: dict | None = None):
        self.screenshot = screenshot or {
            "amount": {"value": 6499.92, "confidence": 0.97},
            "payment_time": {"value": "2026-09-10 19:32:12", "confidence": 0.95},
            "utr": {"value": "611532946151", "confidence": 0.96},
            "upi_id": {"value": "merchant@ptsbi", "confidence": 0.9},
            "payer_name": {"value": "Rohit Tiwari", "confidence": 0.8},
            "payment_status": {"value": "Successful", "confidence": 0.9},
        }
        self.calls = 0
        self.receiver_upi = None  # what the focused screenshot OCR reads (None: no payee UPI printed)
        self.ocr_calls = 0
        self.by_type: dict = {}  # optional: payload per doc_type_hint (screenshot, bank_statement, payment_video)
        self.password = None  # what the focused password pass reads out of a sentence (None: none stated)
        self.password_calls = 0

    async def read_receiver_upi(self, path):
        self.ocr_calls += 1
        v = self.receiver_upi
        return {"value": v, "confidence": 0.97 if v else 0, "evidence_text": f"Sent to: Paytm • {v}" if v else None}

    async def analyze_files(self, files, *, doc_type_hint, context_hint=""):
        from app.ai.extractor import extraction_from_ai

        self.calls += 1
        if self.by_type:  # per document type payloads (statement / video rows), when a test sets them
            payload = self.by_type.get(doc_type_hint, {})
        else:
            payload = self.screenshot if doc_type_hint == "payment_screenshot" else {}
        return extraction_from_ai(payload, doc_type_hint), payload

    async def read_password(self, text):
        self.password_calls += 1
        v = self.password
        return {"value": v, "confidence": 0.9 if v else 0.0, "evidence_text": text if v else None}

    async def classify_betix_reply(self, text, *, context=""):
        return {"outcome": "UNKNOWN", "confidence": 0.0, "refers_to_order_ids": [], "reasoning": "fake"}


@pytest.fixture
def fake_ai(env):
    from app.ai.analyzer import set_analyzer

    a = FakeAnalyzer()
    set_analyzer(a)
    yield a
    set_analyzer(None)


def make_candidates(spec):
    from app.admin.matcher import Candidate
    from app.utils.timeutil import parse_datetime_loose

    out = []
    for c in spec:
        c = dict(c)
        if isinstance(c.get("order_time"), str):
            c["order_time"] = parse_datetime_loose(c["order_time"])
        out.append(Candidate(**c))
    return out


@pytest.fixture
def order_search(env):
    """Returns a setter: order_search([...candidate dicts...])."""
    from app.cases import manager

    holder = {"cands": []}

    async def _search(query, amount=None, when=None):
        holder["query"] = query
        holder["amount"] = amount
        holder["when"] = when
        return make_candidates(holder["cands"])

    def set_(cands):
        holder["cands"] = cands
        manager.set_order_search(_search)
        return holder

    yield set_
    manager.set_order_search(None)


@pytest.fixture
def no_download(env):
    from app.cases import manager

    async def _dl(ev):
        p = Path(ev.local_path or (Path(os.environ["EVIDENCE_DIR"]) / ev.case_id / f"{ev.telegram_message_id}.jpg"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        ev.local_path = str(p)
        ev.downloaded = True
        return p

    manager.set_downloader(_dl)
    yield
    manager.set_downloader(None)


def make_input(message_id, kind="text", text=None, chat_id=111, user_id=111, **kw):
    from app.cases.correlation import IncomingInput

    d = dict(chat_id=chat_id, message_id=message_id, user_id=user_id, username="me", kind=kind, text=text)
    if kind == "photo":
        d.update(file_id=f"photo{message_id}", file_unique_id=f"u{message_id}", mime_type="image/jpeg", size=1234)
    elif kind == "document":
        d.update(
            file_id=f"doc{message_id}",
            file_unique_id=f"u{message_id}",
            mime_type="application/pdf",
            filename="PhonePe_Statement.pdf",
            size=5000,
        )
    elif kind == "video":
        d.update(file_id=f"vid{message_id}", file_unique_id=f"u{message_id}", mime_type="video/mp4", size=900000)
    d.update(kw)
    return IncomingInput(**d)


def make_group_msg(
    message_id,
    text,
    *,
    sender_username=None,
    sender_id=None,
    is_bot=False,
    reply_to=None,
    sent_at=None,
    chat_id=-1009999,
    has_media=False,
):
    from app.telegram.betix_monitor import IncomingGroupMessage
    from app.utils.timeutil import utcnow

    return IncomingGroupMessage(
        chat_id=chat_id,
        message_id=message_id,
        sender_id=sender_id,
        sender_username=sender_username,
        sender_name=sender_username,
        sender_is_bot=is_bot,
        text=text,
        reply_to_message_id=reply_to,
        sent_at=sent_at or utcnow(),
        has_media=has_media,
    )


@pytest.fixture(autouse=True)
async def _fresh_job_runner():
    """Each test gets its own in-process job runner; timers from one test never fire into the next."""
    from app.workers import runner as r

    r.reset_runner()
    yield
    await r.get_runner().shutdown()
    r.reset_runner()
