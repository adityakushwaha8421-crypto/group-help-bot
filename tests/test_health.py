"""/health reports load, not just liveness."""

import httpx


async def test_health_reports_load(env):
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        body = (await c.get("/health")).json()
    assert body["ok"] is True
    assert {"workers", "running", "queued", "lanes", "deferred"} <= set(body["jobs"])
    assert {"size", "busy", "idle", "waiting"} <= set(body["browser"])
    assert {"in_flight", "max"} <= set(body["ai"]) and {"chats", "flood_waits"} <= set(body["telegram"])
