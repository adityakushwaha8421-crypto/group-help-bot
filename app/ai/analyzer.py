"""GPT-5.6 Terra multimodal analysis via the OpenAI Responses API with structured (JSON-schema) outputs."""

from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path
from typing import Any

from app.ai.extractor import Extraction, extraction_from_ai
from app.ai.prompts import (
    BETIX_CLASSIFY_SCHEMA,
    BETIX_CLASSIFY_SYSTEM,
    EXTRACTION_SCHEMA,
    EXTRACTION_SYSTEM,
    PASSWORD_SCHEMA,
    PASSWORD_SYSTEM,
    RECEIVER_UPI_SCHEMA,
    RECEIVER_UPI_SYSTEM,
    STATEMENT_ACCOUNT_SCHEMA,
    STATEMENT_ACCOUNT_SYSTEM,
)
from app.config import get_settings
from app.utils.logging import get_logger

log = get_logger("ai")


class AIUnavailable(Exception):
    pass


def _client():
    s = get_settings()
    if not s.openai_api_key:
        raise AIUnavailable("OPENAI_API_KEY is not configured")
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=s.openai_api_key, timeout=s.openai_timeout_seconds, max_retries=s.openai_max_retries)


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _image_part(path: Path) -> dict:
    return {"type": "input_image", "image_url": _data_url(path), "detail": "high"}


def _pdf_part(path: Path) -> dict:
    return {
        "type": "input_file",
        "filename": path.name,
        "file_data": "data:application/pdf;base64," + base64.b64encode(path.read_bytes()).decode(),
    }


def unavailable_reason(exc: BaseException) -> str | None:
    """A short, honest reason when the OpenAI API itself is the problem (not our request), else None.

    These are outages from the bot's point of view: nothing about the evidence changed, the reader simply did not
    run. Callers hold the case and retry later instead of blaming the screenshot."""
    import openai

    text = str(exc).lower()
    if isinstance(exc, openai.AuthenticationError):
        return "the OpenAI API key was rejected"
    if isinstance(exc, openai.RateLimitError):
        if "credit" in text or "quota" in text or "billing" in text:
            return "no credits left on the OpenAI account"
        return "rate limited by OpenAI"
    if isinstance(exc, openai.APIConnectionError):  # APITimeoutError is one of these
        return "could not reach OpenAI"
    if isinstance(exc, openai.InternalServerError):
        return "OpenAI server error"
    return None


_sem: asyncio.Semaphore | None = None
_in_flight = 0


def _gate() -> asyncio.Semaphore:
    """At most AI_MAX_CONCURRENCY model calls at once; a burst of files queues here instead of at OpenAI."""
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(get_settings().ai_max_concurrency)
    return _sem


def ai_stats() -> dict[str, int]:
    return {"in_flight": _in_flight, "max": get_settings().ai_max_concurrency}


async def _structured(system: str, user_parts: list[dict], schema: dict) -> dict[str, Any]:
    async with _gate():
        return await _structured_now(system, user_parts, schema)


async def _structured_now(system: str, user_parts: list[dict], schema: dict) -> dict[str, Any]:
    global _in_flight
    client = _client()
    s = get_settings()
    _in_flight += 1
    try:
        resp = await client.responses.create(
            model=s.openai_model,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": user_parts},
            ],
            text={"format": {"type": "json_schema", **schema}},
        )
    except Exception as exc:  # noqa: BLE001
        why = unavailable_reason(exc)
        if why:
            raise AIUnavailable(why) from exc
        raise
    finally:
        _in_flight -= 1
    text = getattr(resp, "output_text", None)
    if not text:
        # fall back to walking the output items
        for item in getattr(resp, "output", []) or []:
            for c in getattr(item, "content", []) or []:
                if getattr(c, "type", "") in ("output_text", "text"):
                    text = getattr(c, "text", None)
                    break
    if not text:
        raise AIUnavailable("model returned no text output")
    return json.loads(text)


def _today_india() -> str:
    """The model has no clock: without this it fills a missing year with a guess."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d")


class Analyzer:
    """All AI calls go through here so they can be mocked in tests."""

    async def analyze_files(
        self, files: list[Path], *, doc_type_hint: str, context_hint: str = ""
    ) -> tuple[Extraction, dict]:
        parts: list[dict] = [
            {
                "type": "input_text",
                "text": f"Today's date (India): {_today_india()}. Document type hint: {doc_type_hint}. "
                f"{context_hint}".strip(),
            }
        ]
        for f in files:
            suffix = f.suffix.lower()
            if suffix == ".pdf":
                parts.append(_pdf_part(f))
            elif suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                parts.append(_image_part(f))
        if len(parts) == 1:
            return Extraction(), {"skipped": "no supported files"}
        payload = await _structured(EXTRACTION_SYSTEM, parts, EXTRACTION_SCHEMA)
        return extraction_from_ai(payload, source=doc_type_hint), payload

    async def read_receiver_upi(self, screenshot: Path) -> dict:
        """OCR the payee ("Paid to") UPI off one payment screenshot: {"value", "confidence", "evidence_text"}."""
        parts = [{"type": "input_text", "text": "Payment screenshot:"}, _image_part(screenshot)]
        return (await _structured(RECEIVER_UPI_SYSTEM, parts, RECEIVER_UPI_SCHEMA))["receiver_upi"]

    async def read_password(self, text: str) -> dict:
        """Reason about one operator message and return the PDF password it states, or a null field."""
        parts = [{"type": "input_text", "text": f"Message:\n{text}"}]
        return (await _structured(PASSWORD_SYSTEM, parts, PASSWORD_SCHEMA))["statement_password"]

    async def read_statement_account(self, statement: Path) -> dict:
        """The account number printed in a statement's header: {"value", "confidence", "evidence_text"}."""
        parts = [{"type": "input_text", "text": "Bank statement:"}, _pdf_part(statement)]
        return (await _structured(STATEMENT_ACCOUNT_SYSTEM, parts, STATEMENT_ACCOUNT_SCHEMA))["account_number"]

    async def classify_betix_reply(self, text: str, *, context: str = "") -> dict:
        parts = [{"type": "input_text", "text": f"Context: {context}\n\nMessage:\n{text}"}]
        return await _structured(BETIX_CLASSIFY_SYSTEM, parts, BETIX_CLASSIFY_SCHEMA)


_analyzer: Analyzer | None = None


def get_analyzer() -> Analyzer:
    global _analyzer
    if _analyzer is None:
        _analyzer = Analyzer()
    return _analyzer


def set_analyzer(a: Analyzer | None) -> None:
    global _analyzer
    _analyzer = a
