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
