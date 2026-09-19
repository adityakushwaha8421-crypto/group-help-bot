"""/restart: a clean shutdown, then the same process image is started again (works with or without launchd)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STARTED_AT = time.time()
NOTICE = Path(__file__).resolve().parent.parent / "data" / "restart_notice.json"
_requested = False
_server = None


def bind(server) -> None:
    global _server
    _server = server


def requested() -> bool:
    return _requested


def is_stale(message_ts: float) -> bool:
    """A /restart sent before this process started is the one that started it (Telegram delivers the update
    again when the offset was not confirmed in time). Obeying it would restart for ever."""
    return message_ts < STARTED_AT


def request(chat_id: int) -> None:
    """Remember who asked, then stop the server gracefully; main() starts the new process afterwards."""
    global _requested
    _requested = True
    try:
        NOTICE.parent.mkdir(parents=True, exist_ok=True)
        NOTICE.write_text(json.dumps({"chat_id": chat_id, "at": time.time()}))
    except OSError:
        pass
    if _server is not None:
        _server.should_exit = True  # uvicorn runs the lifespan shutdown: jobs, browser, database


def pop_notice(max_age: float = 300) -> int | None:
    """The chat that asked for the restart, once."""
    try:
        data = json.loads(NOTICE.read_text())
        NOTICE.unlink()
    except (OSError, ValueError):
        return None
    return int(data["chat_id"]) if time.time() - float(data.get("at", 0)) <= max_age else None


def reexec() -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "app.main"])
