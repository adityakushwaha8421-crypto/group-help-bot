"""Telegram HTML building blocks so every operator-facing message looks the same: a bold title line, thin
rules between sections, icons in front of every line, identifiers in monospace. Everything is escaped."""

from __future__ import annotations

import html

RULE = "━━━━━━━━━━━━━━━━━━━━━━"


def esc(value: object) -> str:
    return html.escape("-" if value is None or value == "" else str(value), quote=False)


def b(value: object) -> str:
    return f"<b>{esc(value)}</b>"


def i(value: object) -> str:
    return f"<i>{esc(value)}</i>"


def code(value: object) -> str:
    return f"<code>{esc(value)}</code>"


def title(icon: str, text: str, tag: object | None = None) -> str:
    return f"{icon} <b>{esc(text)}</b>" + (f"  ·  {code(tag)}" if tag else "")


def kv(icon: str, label: str, value: object, *, mono: bool = False, raw: bool = False) -> str:
    shown = value if raw else (code(value) if mono else esc(value))
    return f"{icon} {esc(label)}: {shown}"


def card(head: str, *sections, foot: str | None = None) -> str:
    """head, then each non-empty section separated by a rule, then an optional footer."""
    parts = [head]
    for sec in sections:
        lines = [x for x in (sec or []) if x]
        if lines:
            parts += [RULE, *lines]
    if foot:
        parts += [RULE, foot]
    return "\n".join(parts)


def case_label(case) -> str:
    """What the operator sees as the case id: the Illunise ORDER id once it is known, the internal id before."""
    return case.betex_pay_order_id or case.illunise_order_id or case.case_id


def money(value: float | None) -> str:
    return f"₹{value:,.2f}" if value is not None else "-"


def bullet(items) -> list[str]:
    return [f"  •  {x}" for x in items]


def join_and(items) -> str:
    """['a', 'b', 'c'] -> 'a, b and c'."""
    items = [str(x) for x in items if x]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def para(*blocks) -> str:
    """A short message: blocks separated by a blank line, empties dropped. No rules, no icon columns."""
    out = []
    for blk in blocks:
        if not blk:
            continue
        text = "\n".join(x for x in blk if x) if isinstance(blk, (list, tuple)) else str(blk)
        if text.strip():
            out.append(text)
    return "\n\n".join(out)
