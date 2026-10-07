"""/restart: a clean stop followed by a fresh start, asked for once and confirmed to whoever asked."""

from __future__ import annotations

import time

from app import restart


class Server:
    should_exit = False


def test_request_stops_the_server_and_remembers_who_asked(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "NOTICE", tmp_path / "notice.json")
    monkeypatch.setattr(restart, "_requested", False)
    server = Server()
    restart.bind(server)
    restart.request(4242)
    assert server.should_exit and restart.requested()
    assert restart.pop_notice() == 4242
    assert restart.pop_notice() is None  # confirmed once


def test_an_old_notice_is_not_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(restart, "NOTICE", tmp_path / "notice.json")
    restart.NOTICE.write_text('{"chat_id": 1, "at": 0}')
    assert restart.pop_notice() is None


def test_the_restart_that_started_this_process_is_ignored():
    assert restart.is_stale(restart.STARTED_AT - 5)  # delivered again by Telegram: must not loop
    assert not restart.is_stale(time.time() + 1)


async def test_pull_reports_without_touching_anything_when_it_cannot_fast_forward(monkeypatch):
    calls = []

    async def fake_git(*args, timeout=60):
        calls.append(args)
        if args[0] == "pull":
            return 1, "hint: ...\nfatal: Not possible to fast-forward, aborting."
        return 0, "abc1234"

    monkeypatch.setattr(restart, "_git", fake_git)
    updated, line = await restart.pull_latest()
    assert not updated and "could not update" in line and "fast-forward" in line
    assert ("pull", "--ff-only", "origin", "main") in calls  # never a merge, a rebase or a reset


async def test_pull_says_what_changed(monkeypatch):
    heads = iter(["abc1234", "def5678"])

    async def fake_git(*args, timeout=60):
        return (0, next(heads)) if args[0] == "rev-parse" else (0, "Fast-forward")

    monkeypatch.setattr(restart, "_git", fake_git)
    assert await restart.pull_latest() == (True, "updated abc1234 -> def5678")


async def test_a_pull_that_changes_requirements_installs_them(monkeypatch):
    heads = iter(["abc1234", "def5678"])
    installed = []

    async def fake_git(*args, timeout=60):
        if args[0] == "rev-parse":
            return 0, next(heads)
        if args[0] == "diff":
            return 0, "app/evidence/pdf.py\nrequirements.txt"
        return 0, "Fast-forward"

    async def fake_install():
        installed.append(True)
        return "dependencies installed"

    monkeypatch.setattr(restart, "_git", fake_git)
    monkeypatch.setattr(restart, "install_requirements", fake_install)
    assert await restart.pull_latest() == (True, "updated abc1234 -> def5678; dependencies installed")
    assert installed == [True]


async def test_a_pull_without_requirement_changes_installs_nothing(monkeypatch):
    heads = iter(["abc1234", "def5678"])

    async def fake_git(*args, timeout=60):
        if args[0] == "rev-parse":
            return 0, next(heads)
        return (0, "app/cases/manager.py") if args[0] == "diff" else (0, "Fast-forward")

    async def never():
        raise AssertionError("pip must not run")

    monkeypatch.setattr(restart, "_git", fake_git)
    monkeypatch.setattr(restart, "install_requirements", never)
    assert await restart.pull_latest() == (True, "updated abc1234 -> def5678")
