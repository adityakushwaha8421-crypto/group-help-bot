"""/restart: a clean shutdown, then the same process image is started again (works with or without launchd)."""

from __future__ import annotations

import asyncio
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


ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = "app/db/migrations/versions/"


async def _git(*args: str, timeout: float = 60) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 1, "timed out"
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def pull_latest() -> tuple[bool, str]:
    """Fast-forward to GitHub's main before restarting. (updated?, one line for the operator.) Local changes or
    local commits are never overwritten: then nothing is pulled and the line says so."""
    if not (ROOT / ".git").exists():
        return False, "not a git checkout - code unchanged"
    before = (await _git("rev-parse", "--short", "HEAD"))[1]
    code, out = await _git("pull", "--ff-only", "origin", "main")
    if code != 0:
        last = out.splitlines()[-1][:120] if out else "unknown error"
        return False, f"could not update ({last}) - code unchanged"
    after = (await _git("rev-parse", "--short", "HEAD"))[1]
    if before == after:
        return False, f"already up to date ({after})"
    line = f"updated {before} -> {after}"
    changed = (await _git("diff", "--name-only", before, after))[1].splitlines()
    if "requirements.txt" in changed:
        line += "; " + await install_requirements()
    if any(f.startswith(MIGRATIONS) for f in changed):
        line += "; " + await migrate_database()
    return True, line


async def _run(*cmd: str, timeout: float, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=ROOT, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 1, "timed out"
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def install_requirements() -> str:
    """requirements.txt changed in the pull: install it into THIS interpreter's environment before restarting."""
    code, out = await _run(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt", timeout=600)
    if code == 0:
        return "dependencies installed"
    last = out.splitlines()[-1][:120] if out else "unknown error"
    return f"dependency install FAILED ({last}) - run: pip install -r requirements.txt"


async def migrate_database() -> str:
    """A migration file arrived in the pull: bring the database schema to it (alembic upgrade head) before the
    new code, which expects the new columns, starts. A SQLite dev database is created by the app itself."""
    from app.config import get_settings

    s = get_settings()
    if s.is_sqlite:
        return "schema created at start (sqlite)"
    # alembic reads DATABASE_URL from the environment (else alembic.ini's default): the bot's own database, from
    # its .env, is handed over explicitly - the .env is not exported to the environment.
    env = {**os.environ, "DATABASE_URL": s.database_url}
    code, out = await _run(sys.executable, "-m", "alembic", "upgrade", "head", timeout=300, env=env)
    if code == 0:
        return "database migrated"
    last = out.splitlines()[-1][:120] if out else "unknown error"
    return f"database migration FAILED ({last}) - run: alembic upgrade head"


def reexec() -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "app.main"])
