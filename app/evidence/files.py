"""Evidence file storage: per-case directory, safe filenames, retention."""

from __future__ import annotations

import re
import shutil
from datetime import timedelta
from pathlib import Path

from app.config import get_settings
from app.utils.timeutil import utcnow

SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def case_dir(case_id: str) -> Path:
    d = Path(get_settings().evidence_dir) / case_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_name(name: str | None, fallback: str) -> str:
    n = SAFE.sub("_", name or "").strip("._")
    return n or fallback


def target_path(case_id: str, message_id: int, filename: str | None, ext: str) -> Path:
    base = safe_name(filename, f"file{ext}")
    if not Path(base).suffix and ext:
        base += ext
    return case_dir(case_id) / f"{message_id}_{base}"


def retention_expiry():
    days = get_settings().evidence_retention_days
    return utcnow() + timedelta(days=days) if days > 0 else None


def purge_expired(now=None) -> int:
    """Delete case directories older than the retention period. Returns number removed."""
    s = get_settings()
    if s.evidence_retention_days <= 0:
        return 0
    now = now or utcnow()
    root = Path(s.evidence_dir)
    removed = 0
    if not root.exists():
        return 0
    for d in root.iterdir():
        if d.is_dir():
            age = now.timestamp() - d.stat().st_mtime
            if age > s.evidence_retention_days * 86400:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
    return removed
