"""Hashing for idempotency keys."""

from __future__ import annotations

import hashlib


def idempotency_key(*parts: object) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]
