"""Login to the Illunise admin panel. Never bypasses OTP/2FA/captcha: those trigger a manual auth step.

Manual auth:  python -m app.admin.login --manual
  opens a headed browser; log in yourself; the session (cookies) is saved to ADMIN_STORAGE_STATE_PATH and
  reused by the workers until it expires.
"""

from __future__ import annotations

import argparse
import asyncio

from app.admin.browser import AdminBrowser, LoginFailed, ManualAuthRequired
from app.config import get_settings
from app.utils.logging import configure_logging, get_logger

log = get_logger("admin.login")


async def is_logged_in(browser: AdminBrowser) -> bool:
    sel = browser.selectors["login"]
    marker = sel.get("login_url_marker", "/admin/login")
    orders_path = browser.selectors["orders"]["path"]
    await browser.page.goto(browser.url(orders_path), wait_until="domcontentloaded")
    await browser.page.wait_for_load_state("networkidle")
    if marker in browser.page.url:
        return False
    if await browser.any_visible(sel.get("username", ""), 800):
        return False
    return True


async def ensure_logged_in(browser: AdminBrowser) -> None:
    s = get_settings()
    if await is_logged_in(browser):
        log.info("admin session valid (storage state)")
        return
    if s.admin_auth_mode == "manual":
        raise ManualAuthRequired(
            "ADMIN_AUTH_MODE=manual and no valid saved session. Run: python -m app.admin.login --manual"
        )
    if not s.illunise_admin_username or not s.illunise_admin_password:
        raise LoginFailed("ILLUNISE_ADMIN_USERNAME / ILLUNISE_ADMIN_PASSWORD not configured")
    sel = browser.selectors["login"]
    page = browser.page
    await page.goto(browser.url(sel["path"]), wait_until="domcontentloaded")
    if await browser.any_visible(sel.get("second_factor", ""), 800):
        raise ManualAuthRequired("second factor / captcha present on login page")
    user_loc = await browser.first_locator(sel["username"])
    await user_loc.fill(s.illunise_admin_username)
    pwd_loc = await browser.first_locator(sel["password"])
    await pwd_loc.fill(s.illunise_admin_password)
    submit = await browser.first_locator(sel["submit"])
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=s.admin_nav_timeout_ms):
        await submit.click()
    await page.wait_for_load_state("networkidle")
    if await browser.any_visible(sel.get("second_factor", ""), 1000):
        raise ManualAuthRequired("second factor / captcha requested after password")
    still_on_login = sel.get("login_url_marker", "/admin/login") in page.url or await browser.any_visible(
        sel["username"], 800
    )
    if still_on_login:
        # Error text is only meaningful on the login page itself: the dashboard shows "FAILED" order counters.
        reason = (
            "error message shown"
            if await browser.any_visible(sel.get("error", ""), 1000)
            else "still on the login page"
        )
        art = await browser.save_debug("login-failed")
        raise LoginFailed(f"login rejected by the admin panel ({reason}; see {art['screenshot']})")
    await browser.save_state()
    log.info("admin login successful; storage state saved")


async def manual_login() -> None:
    """Headed browser; the operator logs in by hand; we persist the session."""
    async with AdminBrowser(headless=False) as browser:
        sel = browser.selectors["login"]
        await browser.page.goto(browser.url(sel["path"]))
        print("\nA browser window is open. Log in to the admin panel (including any OTP/captcha).")
        print("When you can see the orders page, press ENTER here to save the session...")
        await asyncio.get_event_loop().run_in_executor(None, input)
        if await is_logged_in(browser):
            await browser.save_state()
            print(f"Session saved to {browser.state_path}")
        else:
            print("It does not look like you are logged in. Nothing saved.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--manual", action="store_true", help="open a headed browser and save the session after manual login"
    )
    ap.add_argument("--check", action="store_true", help="verify the saved session / automatic login works")
    args = ap.parse_args()
    configure_logging(get_settings().log_level)
    if args.manual:
        asyncio.run(manual_login())
    else:

        async def _check():
            """Login check. Prints only non-sensitive facts: never a credential, cookie or token."""
            async with AdminBrowser() as b:
                sel = b.selectors
                had_state = b.state_path.exists()
                print(f"1. saved session present before check: {had_state}")
                await ensure_logged_in(b)
                print("2. authentication: OK (no login form, no error banner, no second-factor prompt)")
                page = b.page
                await page.goto(b.url(sel["orders"]["path"]), wait_until="domcontentloaded")
                await page.wait_for_load_state("networkidle")
                on_login = sel["login"].get("login_url_marker", "/admin/login") in page.url
                title = (await page.title())[:80]
                print(
                    f"3. orders page: {'NOT accessible (redirected to login)' if on_login else 'accessible'} — url={page.url} title={title!r}"
                )
                tables = await page.locator(sel["orders"]["table"]).count()
                search = await page.locator(sel["orders"]["search_input"]).count()
                print(
                    f"4. orders page structure: tables={tables} search_inputs={search} (0 tables means the selectors need tuning)"
                )
                if tables:
                    headers = [
                        h.strip()
                        for h in await page.locator(sel["orders"]["table"])
                        .first.locator(sel["orders"]["header_cells"])
                        .all_inner_texts()
                    ]
                    rows = await page.locator(sel["orders"]["table"]).first.locator(sel["orders"]["rows"]).count()
                    print(f"   column headers: {headers} | rows on first page: {rows}")
                await b.save_state()
                print(f"5. session saved to {b.state_path} (reusable: {b.state_path.exists()})")
                print(
                    "ILLUNISE LOGIN: PASS" if not on_login else "ILLUNISE LOGIN: FAIL (orders page redirected to login)"
                )

        asyncio.run(_check())


if __name__ == "__main__":
    main()
