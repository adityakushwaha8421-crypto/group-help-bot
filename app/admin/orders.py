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


async def enrich_candidates(
    browser: AdminBrowser,
    candidates: list[Candidate],
    *,
    evidence_amount: float | None = None,
    evidence_time=None,
    max_pages: int | None = None,
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

    def priority(c: Candidate) -> tuple[int, float]:
        amt_match = (
            0
            if (evidence_amount is not None and c.amount is not None and abs(c.amount - evidence_amount) <= tol)
            else 1
        )
        if evidence_time is not None and c.order_time is not None:
            from app.utils.timeutil import ensure_utc

            distance = abs((ensure_utc(evidence_time) - ensure_utc(c.order_time)).total_seconds())
        else:
            distance = -(c.order_time.timestamp() if c.order_time else 0)
        return (amt_match, distance)

    for c in sorted(candidates, key=priority)[:limit]:
        oid = c.illunise_order_id or c.betex_order_id
        if not oid:
            continue
        try:
            fields = await read_view_fields(browser, oid)
            apply_view_fields(c, fields, view_map, s.timezone)
        except Exception as exc:  # noqa: BLE001
            log.warning("view page read failed", order_id=oid, error=str(exc)[:120])
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
    if sel.get("search_query_param"):
        await page.goto(
            browser.url(sel["path"]) + sel["search_query_param"].format(query=quote(query, safe="")),
            wait_until="domcontentloaded",
        )
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
    for _ in range(int(sel.get("max_pages", 1))):
        try:
            headers, rows = await _read_table(browser)
        except LayoutChanged:
            art = await browser.save_debug("orders-layout")
            raise LayoutChanged(f"orders table not found; debug artifacts: {art}")
        for cells in rows:
            candidates.append(row_to_candidate(headers, cells, column_map, s.betex_order_id_pattern, s.timezone))
        nxt = sel.get("next_page")
        if not nxt or not await browser.any_visible(nxt, 500):
            break
        loc = await browser.first_locator(nxt, 2000)
        await loc.click()
        await page.wait_for_load_state("networkidle")
    # The list view has no mobile/name column, so a mobile search cannot be re-checked against the row text.
    # The View page (enrichment below) supplies the mobile; the matcher then verifies it exactly.
    if enrich:
        candidates = await enrich_candidates(
            browser, candidates, evidence_amount=evidence_amount, evidence_time=evidence_time
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
