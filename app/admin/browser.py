"""Playwright browser layer for the Illunise admin panel. Persistent storage state, configurable selectors."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings
from app.utils.logging import get_logger
from app.utils.timeutil import utcnow

log = get_logger("admin.browser")


class AdminError(Exception):
    pass


class LoginFailed(AdminError):
    pass


class ManualAuthRequired(AdminError):
    """OTP / captcha / 2FA detected, or ADMIN_AUTH_MODE=manual without a saved session."""


class LayoutChanged(AdminError):
    """Expected selectors not found: the site layout probably changed."""


_selectors_cache: dict[str, Any] | None = None


def load_selectors(path: str | None = None) -> dict[str, Any]:
    global _selectors_cache
    p = Path(path or get_settings().admin_selectors_file)
    if _selectors_cache is None or path:
        with open(p, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not path:
            _selectors_cache = data
        return data
    return _selectors_cache


class AdminBrowser:
    """Owns a Chromium context with persisted cookies/local storage."""

    def __init__(self, headless: bool | None = None, shared_browser=None):
        """shared_browser: an already-launched Playwright Browser (the pool's). Then this object owns only its
        own context + page, and closing it leaves the browser running for the others."""
        s = get_settings()
        self.settings = s
        self.headless = s.admin_headless if headless is None else headless
        self.selectors = load_selectors()
        self.state_path = Path(s.admin_storage_state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._pw = None
        self._browser = shared_browser
        self._owns_browser = shared_browser is None
        self.context = None
        self.page = None

    async def __aenter__(self):
        if self._owns_browser:
            from playwright.async_api import async_playwright

            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self.headless)
        kwargs: dict[str, Any] = {"viewport": {"width": 1400, "height": 1000}}
        if self.state_path.exists():
            kwargs["storage_state"] = str(self.state_path)
        self.context = await self._browser.new_context(**kwargs)
        self.context.set_default_timeout(self.settings.admin_nav_timeout_ms)
        self.page = await self.context.new_page()
        return self

    async def __aexit__(self, *exc):
        try:
            if self.context:
                await self.context.close()
            if self._browser and self._owns_browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    def is_alive(self) -> bool:
        """The page can still be driven (its context and browser are open)."""
        try:
            return bool(self.page) and not self.page.is_closed() and self._browser.is_connected()
        except Exception:  # noqa: BLE001
            return False

    def url(self, path: str) -> str:
        return self.settings.illunise_admin_base_url.rstrip("/") + "/" + path.lstrip("/")

    async def save_state(self) -> None:
        await self.context.storage_state(path=str(self.state_path))

    async def save_debug(self, label: str) -> dict[str, str]:
        d = Path(self.settings.admin_debug_artifacts_dir)
        d.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        png = d / f"{stamp}-{label}.png"
        html = d / f"{stamp}-{label}.html"
        try:
            await self.page.screenshot(path=str(png), full_page=True)
            html.write_text(await self.page.content(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save debug artifacts", error=str(exc))
        return {"screenshot": str(png), "html": str(html)}

    async def any_visible(self, selector_list: str, timeout_ms: int = 1500) -> bool:
        """selector_list: comma separated selectors (CSS or text=...). True if any is visible."""
        if not selector_list:
            return False
        for sel in [x.strip() for x in selector_list.split(",") if x.strip()]:
            try:
                loc = self.page.locator(sel).first
                await loc.wait_for(state="visible", timeout=timeout_ms)
                return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def first_locator(self, selector_list: str, timeout_ms: int | None = None):
        timeout_ms = timeout_ms or self.settings.admin_nav_timeout_ms
        deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
        sels = [x.strip() for x in selector_list.split(",") if x.strip()]
        while asyncio.get_event_loop().time() < deadline:
            for sel in sels:
                loc = self.page.locator(sel).first
                try:
                    if await loc.count() and await loc.is_visible():
                        return loc
                except Exception:  # noqa: BLE001
                    continue
            await asyncio.sleep(0.25)
        raise LayoutChanged(f"none of the selectors were found: {selector_list}")
