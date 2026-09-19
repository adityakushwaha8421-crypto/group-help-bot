"""Search /admin/orders and extract candidate orders generically from the results table.

Column headers are mapped to canonical fields through config/admin_selectors.yaml -> columns.
Unknown columns are preserved in Candidate.raw so nothing is lost for manual review."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from json import JSONDecodeError
from urllib.parse import quote

from app.admin.browser import AdminBrowser, LayoutChanged
from app.admin.login import ensure_logged_in
from app.admin.matcher import Candidate
from app.ai.extractor import normalize_amount
from app.config import get_settings
from app.utils.logging import configure_logging, get_logger
from app.utils.timeutil import parse_datetime_loose

log = get_logger("admin.orders")


def _norm_header(h: str) -> str:
    return re.sub(r"[\s_\-.:#]+", " ", (h or "").strip().lower()).strip()


def build_column_index(headers: list[str], column_map: dict[str, list[str]]) -> dict[str, int]:
    """canonical field -> column index (first header alias that matches)."""
    normalized = [_norm_header(h) for h in headers]
    index: dict[str, int] = {}
    for field, aliases in column_map.items():
        for alias in aliases:
            a = _norm_header(alias)
            for i, h in enumerate(normalized):
                if h == a and i not in index.values():
                    index[field] = i
                    break
            if field in index:
                break
    return index


def row_to_candidate(
    headers: list[str], cells: list[str], column_map: dict[str, list[str]], betex_pattern: re.Pattern[str], tz: str
) -> Candidate:
    idx = build_column_index(headers, column_map)
    raw = {headers[i] if i < len(headers) else f"col{i}": c for i, c in enumerate(cells)}

    def get(field: str) -> str | None:
        i = idx.get(field)
        if i is None or i >= len(cells):
            return None
        return clean_cell(cells[i])

    betex = get("betex_order_id")
    if not betex or not betex_pattern.search(betex):
        # the betex/merchant order id may live in any cell (e.g. combined "Ref" column)
        for c in cells:
            m = betex_pattern.search(c or "")
            if m:
                betex = m.group(0).upper()
                break
    return Candidate(
        illunise_order_id=get("illunise_order_id") or betex,
        # In this panel the merchant order id (ILLUN-…) IS what Betix knows as MerchantOrderNo, i.e. the
        # "Betex Pay order id" we post. There is no separate Betix id on the Illunise side.
        betex_order_id=betex.upper() if betex else None,
        registration_number=get("registration_number"),
        amount=normalize_amount(get("amount")),
        order_time=parse_datetime_loose(get("created_at") or get("paid_at"), tz),
        status=get("status"),
        utr=get("utr"),
        upi_id=get("upi_id"),
        payer_name=get("payer_name"),
        gateway=get("gateway"),
        raw=raw,
    )


_EMPTY = {"", "—", "-", "–", "n/a", "na", "null", "none"}


def clean_cell(value: str | None) -> str | None:
    """Trim, drop placeholder dashes, and strip leading status icons ('◌ Pending' -> 'Pending')."""
    if value is None:
        return None
    v = re.sub(r"^[^\w₹$€.+-]+", "", value.strip()).strip()
    return None if v.lower() in _EMPTY else v


def _norm_label(label: str) -> str:
    return re.sub(r"[\s_\-.:#/]+", " ", (label or "").strip().lower()).strip()


async def read_view_fields(browser: AdminBrowser, order_id: str) -> dict[str, str]:
    """Open /admin/orders/<id> and return every label -> value pair on the page (raw, unmapped)."""
    sel = browser.selectors["orders"]
    path = sel.get("detail_path", "/admin/orders/{order_id}").format(order_id=order_id)
    page = browser.page
    await page.goto(browser.url(path), wait_until="domcontentloaded")
    await page.wait_for_load_state("networkidle")
    pairs = await page.evaluate(
        """() => { const out = [];
          document.querySelectorAll('dl').forEach(dl => { const dts=[...dl.querySelectorAll('dt')], dds=[...dl.querySelectorAll('dd')];
            dts.forEach((dt,i) => out.push([dt.innerText.trim(), (dds[i]||{}).innerText?.trim() || ''])); });
          document.querySelectorAll('table tr').forEach(tr => { const c=[...tr.children].map(x=>x.innerText.trim()); if (c.length>=2) out.push([c[0], c.slice(1).join(' | ')]); });
          document.querySelectorAll('[class*=label],[class*=key],[class*=meta] span,[class*=field]').forEach(el => { const v=el.nextElementSibling;
            if (v && el.innerText && el.innerText.length < 40) out.push([el.innerText.trim(), v.innerText.trim()]); });
          return out; }"""
    )
    fields: dict[str, str] = {}
    for k, v in pairs:
        if k and k not in fields:
            fields[k] = v
    # The gateway's JSON responses are embedded in <pre> blocks ("View raw BetixPay response",
    # "View full creation response JSON"). They carry the Betix platOrderNo, merchantOrderNo, refNo (UTR),
    # paidAmount and status — the only place the Betix-side identifier exists on the Illunise side.
    # text_content, not inner_text: the <pre> blocks sit inside collapsed <details> and are not "visible".
    for block in await page.locator("pre").all_text_contents():
        try:
            data = json.loads(block)
        except JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        for key in ("platOrderNo", "merchantOrderNo", "refNo", "paidAmount", "orderAmount", "status", "fee", "method"):
            if key in data and data[key] not in (None, ""):
                fields.setdefault(f"gateway.{key}", str(data[key]))
    return fields


def apply_view_fields(cand: Candidate, fields: dict[str, str], view_map: dict[str, list[str]], tz: str) -> Candidate:
    """Fill a candidate from its View page. View values win over list values (they are the full record)."""
    norm = {_norm_label(k): v for k, v in fields.items()}

    def get(field: str) -> str | None:
        for alias in view_map.get(field, []):
            v = norm.get(_norm_label(alias))
            if v is not None:
                v = clean_cell(v)
                if v:
                    return v
        return None

    mobile = get("registration_number")
    if mobile:
        digits = re.sub(r"\D", "", mobile)
        cand.registration_number = digits[-10:] if len(digits) >= 10 else mobile
    cand.payer_name = get("payer_name") or cand.payer_name
    cand.gateway = get("gateway") or cand.gateway
    plat = fields.get("gateway.platOrderNo")
    if plat and get_settings().plat_order_pattern.fullmatch(plat.strip()):
        cand.betix_plat_order_no = plat.strip()
    ref = fields.get("gateway.refNo")
    if ref and not cand.utr:
        cand.utr = ref.strip()
    padded = normalize_amount(get("padded_amount"))
    if padded is not None:
        cand.padded_amount = padded
    cand.utr = get("utr") or cand.utr
    cand.status = get("status") or cand.status
    amt = normalize_amount(get("amount"))
    if amt is not None:
        cand.amount = amt
    created = parse_datetime_loose(get("created_at"), tz)
    if created:
        cand.order_time = created
    cand.raw.update({f"view:{k}": v for k, v in fields.items() if k not in ("Callback URL", "Return URL")})
    return cand


# The panel shows minutes only: an order created in the payment's own minute can read as "after" it. One minute
# of slack covers that; anything later was created AFTER the payment and is never read.
SKEW_MINUTES = 1


def search_dates(evidence_time, window_minutes: int, tz: str) -> tuple[str, str] | None:
    """The panel date range (YYYY-MM-DD, panel-local) that can hold the order for a payment at `evidence_time`:
    the payment's own day - plus the day before when the window reaches back across midnight. An order is
    created BEFORE its payment, so no later day is ever needed."""
    if evidence_time is None:
        return None
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from app.utils.timeutil import ensure_utc

    local = ensure_utc(evidence_time).astimezone(ZoneInfo(tz))
    start = local - timedelta(minutes=window_minutes)
    return start.strftime("%Y-%m-%d"), local.strftime("%Y-%m-%d")


def search_url(sel: dict, base: str, query: str, dates: tuple[str, str] | None = None, page: int = 1) -> str:
    url = base + sel["search_query_param"].format(query=quote(query, safe=""))
    if dates and sel.get("date_query_param"):
        url += sel["date_query_param"].format(from_date=dates[0], to_date=dates[1])
    if page > 1 and sel.get("page_query_param"):
        url += sel["page_query_param"].format(page=page)
    return url


def _lead_minutes(c: Candidate, evidence_time) -> float | None:
    """Minutes the order was created BEFORE the payment (negative: created after it)."""
    if evidence_time is None or c.order_time is None:
        return None
    from app.utils.timeutil import ensure_utc

    return (ensure_utc(evidence_time) - ensure_utc(c.order_time)).total_seconds() / 60


def closeness_key(c: Candidate, evidence_time, evidence_amount: float | None, tol: float):
    """Reading order: created before the payment first, amount matches first, then closest to the payment."""
    lead = _lead_minutes(c, evidence_time)
    after = 0 if lead is None or lead >= -SKEW_MINUTES else 1
    amt = 0 if (evidence_amount is not None and c.amount is not None and abs(c.amount - evidence_amount) <= tol) else 1
    return (after, amt, abs(lead) if lead is not None else 1e9)


def narrow_candidates(
    candidates: list[Candidate],
    evidence_time,
    *,
    window_minutes: int,
    keep_closest: int,
    dates=None,
    tz: str = "Asia/Kolkata",
) -> list[Candidate]:
    """From everything the panel returned, the orders worth reading: created within `window_minutes` before the
    payment (SKEW_MINUTES of slack for the panel's minute resolution). Rows outside the requested dates are dropped first - the
    result must not depend on the panel honouring its filter. When nothing is inside the window, the few closest
    are kept so the manual-review message can still name them; they can never auto-match (the matcher's time
    rule sees to that)."""
    if evidence_time is None:
        return candidates
    from zoneinfo import ZoneInfo

    from app.utils.timeutil import ensure_utc

    pool = candidates
    if dates:
        kept = []
        for c in pool:
            day = ensure_utc(c.order_time).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d") if c.order_time else None
            if day is None or dates[0] <= day <= dates[1]:
                kept.append(c)
        if len(kept) != len(pool):
            log.warning("panel returned orders outside the requested dates; dropped", dropped=len(pool) - len(kept))
        pool = kept
    inside = []
    for c in pool:
        lead = _lead_minutes(c, evidence_time)
        if lead is None or -SKEW_MINUTES <= lead <= window_minutes:
            inside.append(c)
    if inside:
        return inside

    def distance(c: Candidate) -> float:
        lead = _lead_minutes(c, evidence_time)
        return abs(lead) if lead is not None else 1e9

    return sorted(pool, key=distance)[:keep_closest]


def is_clear_match(c: Candidate, query: str, evidence_time, evidence_amount, tol: float, window: int) -> bool:
    """After its View page was read: this is the customer's own order for this payment - same registered mobile,
    same amount, created shortly before the payment. Nothing further needs reading."""
    digits = re.sub(r"\D", "", query or "")
    lead = _lead_minutes(c, evidence_time)
    return bool(
        len(digits) == 10
        and c.registration_number
        and re.sub(r"\D", "", c.registration_number)[-10:] == digits
        and evidence_amount is not None
        and c.amount is not None
        and abs(c.amount - evidence_amount) <= tol
        and lead is not None
        and 0 <= lead <= window
    )


async def enrich_candidates(
    browser: AdminBrowser,
    candidates: list[Candidate],
    *,
    evidence_amount: float | None = None,
    evidence_time=None,
    max_pages: int | None = None,
    query: str = "",
) -> list[Candidate]:
    """Read the View page of the most promising candidates: amount matches (within the padded-amount tolerance)
    first, closest to the payment time next, newest otherwise."""
    s = get_settings()
    sel = browser.selectors["orders"]
    view_map = browser.selectors.get("view_fields", {})
    limit = int(max_pages if max_pages is not None else sel.get("max_detail_pages", 15))
    if not view_map or limit <= 0:
        return candidates
    tol = s.order_amount_tolerance + 1e-9

    ordered = sorted(candidates, key=lambda c: closeness_key(c, evidence_time, evidence_amount, tol))
    for n, c in enumerate(ordered[:limit], start=1):
        oid = c.illunise_order_id or c.betex_order_id
        if not oid:
            continue
        try:
            fields = await read_view_fields(browser, oid)
            apply_view_fields(c, fields, view_map, s.timezone)
        except Exception as exc:  # noqa: BLE001
            log.warning("view page read failed", order_id=oid, error=str(exc)[:120])
            continue
        if is_clear_match(c, query, evidence_time, evidence_amount, tol, s.order_search_window_minutes):
            log.info("clear match; no further orders read", order_id=oid, read=n, of=len(ordered))
            break
    return candidates


async def _read_table(browser: AdminBrowser) -> tuple[list[str], list[list[str]]]:
    sel = browser.selectors["orders"]
    table = await browser.first_locator(sel["table"])
    headers = [h.strip() for h in await table.locator(sel["header_cells"]).all_inner_texts()]
    rows: list[list[str]] = []
    for row in await table.locator(sel["rows"]).all():
        cells = [c.strip() for c in await row.locator(sel["cells"]).all_inner_texts()]
        if cells and any(cells):
            rows.append(cells)
    if not headers:
        # some panels use the first row as headers
        if rows:
            headers, rows = rows[0], rows[1:]
    return headers, rows


async def search_orders(
    browser: AdminBrowser, query: str, *, evidence_amount: float | None = None, evidence_time=None, enrich: bool = True
) -> list[Candidate]:
    """Search the orders page and return ALL candidates (all pages up to max_pages), enriched from their View pages."""
    s = get_settings()
    sel = browser.selectors["orders"]
    page = browser.page
    dates = (
        search_dates(evidence_time, s.order_search_window_minutes, s.timezone) if sel.get("date_query_param") else None
    )
    by_url = bool(sel.get("search_query_param"))
    if by_url:
        await page.goto(search_url(sel, browser.url(sel["path"]), query, dates), wait_until="domcontentloaded")
    else:
        await page.goto(browser.url(sel["path"]), wait_until="domcontentloaded")
        box = await browser.first_locator(sel["search_input"])
        await box.fill("")
        await box.fill(query)
        if sel.get("search_submit"):
            btn = await browser.first_locator(sel["search_submit"])
            await btn.click()
        else:
            await box.press("Enter")
    await page.wait_for_load_state("networkidle")
    if sel.get("empty_marker") and await browser.any_visible(sel["empty_marker"], 800):
        return []
    candidates: list[Candidate] = []
    column_map = browser.selectors.get("columns", {})
    max_pages = int(sel.get("max_pages", 1))
    for page_no in range(1, max_pages + 1):
        try:
            headers, rows = await _read_table(browser)
        except LayoutChanged:
            if page_no > 1:
                break  # past the last page
            art = await browser.save_debug("orders-layout")
            raise LayoutChanged(f"orders table not found; debug artifacts: {art}")
        for cells in rows:
            candidates.append(row_to_candidate(headers, cells, column_map, s.betex_order_id_pattern, s.timezone))
        nxt = sel.get("next_page")
        if not nxt or not await browser.any_visible(nxt, 500):
            break
        if page_no == max_pages:
            log.warning("order list truncated at the page cap", pages=max_pages, rows=len(candidates))
            break
        if by_url and sel.get("page_query_param"):
            nxt_url = search_url(sel, browser.url(sel["path"]), query, dates, page_no + 1)
            await page.goto(nxt_url, wait_until="domcontentloaded")
        else:
            loc = await browser.first_locator(nxt, 2000)
            await loc.click()
        await page.wait_for_load_state("networkidle")
    listed = len(candidates)
    candidates = narrow_candidates(
        candidates,
        evidence_time,
        window_minutes=s.order_search_window_minutes,
        keep_closest=s.order_search_keep_closest,
        dates=dates,
        tz=s.timezone,
    )
    log.info("order search", dates=dates, listed=listed, kept=len(candidates))
    # The list view has no mobile/name column, so a mobile search cannot be re-checked against the row text.
    # The View page (enrichment below) supplies the mobile; the matcher then verifies it exactly.
    if enrich:
        candidates = await enrich_candidates(
            browser, candidates, evidence_amount=evidence_amount, evidence_time=evidence_time, query=query
        )
    return candidates


async def find_candidates(query: str, evidence_amount: float | None = None, evidence_time=None) -> list[Candidate]:
    """Search with a tab borrowed from the shared browser pool (see app.admin.pool): no browser launch, no fresh
    login per search, and a cap on how many searches run at the same time."""
    from app.admin.pool import get_pool

    async with get_pool().page() as browser:
        await ensure_logged_in(browser)
        return await search_orders(browser, query, evidence_amount=evidence_amount, evidence_time=evidence_time)


def main() -> None:
    ap = argparse.ArgumentParser(description="Search the admin orders page and print candidates (for selector tuning)")
    ap.add_argument("query")
    args = ap.parse_args()
    configure_logging(get_settings().log_level)
    cands = asyncio.run(find_candidates(args.query))
    for c in cands:
        d = c.__dict__.copy()
        d["order_time"] = c.order_time.isoformat() if c.order_time else None
        print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
    print(f"{len(cands)} candidate(s)")


if __name__ == "__main__":
    main()
