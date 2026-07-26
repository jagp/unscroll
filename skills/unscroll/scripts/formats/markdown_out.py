"""Markdown dialogue-script transcript writer (SKILL.md Phase 5).

Renders the canonical document as a paginated TV/film-style dialogue script:
a ``════`` page-header block, ``── MONTH DAY, YEAR ──`` date section headers,
``── [N-hour gap] ──`` / ``── [N-day gap] ──`` silence markers, and bracketed
media placeholders. Pages break at each calendar-month boundary or after 150
messages (whichever comes first), preferring a natural gap. Messages with a null
timestamp are collected into a trailing ``── UNDATED ──`` section so the writer
never crashes on missing time data.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from formats.text_out import body_lines, media_placeholder, parse_ts, sender_name

_RULE = "═" * 52
_DIVIDER = "─"
_INDENT = "  "

# Pagination / gap thresholds.
_PAGE_MSG_CAP = 150
_PAGE_MSG_HARD_CAP = 200
_GAP_HOUR = timedelta(hours=4)
_GAP_DAY = timedelta(hours=24)
_GAP_NEW_SECTION = timedelta(days=7)
_MONTHS = (
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
    "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
)


def write_markdown(doc: dict, path: str) -> None:
    """Write the paginated dialogue-script transcript of ``doc`` to ``path``."""
    messages = list(doc.get("messages", []))
    dated = [m for m in messages if parse_ts(m.get("timestamp")) is not None]
    undated = [m for m in messages if parse_ts(m.get("timestamp")) is None]

    pages = _paginate(dated)
    if undated:
        # Host the trailing UNDATED section on its own final page so it still
        # gets a page header and never disturbs dated pagination.
        pages.append([])
    if not pages:
        pages = [[]]

    total_pages = len(pages)
    lines: list[str] = []
    for page_no, page_msgs in enumerate(pages, start=1):
        lines.extend(_page_header(doc, page_no, total_pages))
        lines.append("")
        is_last = page_no == total_pages
        lines.extend(_render_page(page_msgs, undated if is_last else []))
        if page_no != total_pages:
            lines.append("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")


def _paginate(dated: list[dict]) -> list[list[dict]]:
    """Group timestamped messages into pages by month boundary or message cap.

    Prefer breaking at a natural gap (>= 4h) once the soft cap is reached, but
    force a break at a month boundary or the hard cap so dense conversations
    still paginate.
    """
    pages: list[list[dict]] = []
    current: list[dict] = []
    page_month: tuple[int, int] | None = None
    prev_ts: datetime | None = None

    for msg in dated:
        ts = parse_ts(msg["timestamp"])
        month = (ts.year, ts.month)
        if current:
            gap = (ts - prev_ts) if prev_ts else timedelta(0)
            month_break = page_month is not None and month != page_month
            cap_break = len(current) >= _PAGE_MSG_CAP and gap >= _GAP_HOUR
            hard_break = len(current) >= _PAGE_MSG_HARD_CAP
            if month_break or cap_break or hard_break:
                pages.append(current)
                current = []
                page_month = None
        current.append(msg)
        if page_month is None:
            page_month = month
        prev_ts = ts

    if current:
        pages.append(current)
    return pages


def _render_page(page_msgs: list[dict], undated: list[dict]) -> list[str]:
    """Render one page's message blocks plus any trailing undated section."""
    lines: list[str] = []
    prev_ts: datetime | None = None
    prev_date = None

    for msg in page_msgs:
        ts = parse_ts(msg["timestamp"])
        if prev_ts is not None:
            marker = _gap_marker(prev_ts, ts)
            if marker:
                lines.append(marker)
                lines.append("")
        if prev_date != ts.date():
            lines.append(_date_header(ts))
            lines.append("")
        lines.extend(_message_block(msg))
        lines.append("")
        prev_ts = ts
        prev_date = ts.date()

    if undated:
        lines.append(_section("UNDATED"))
        lines.append("")
        for msg in undated:
            lines.extend(_message_block(msg))
            lines.append("")

    return lines


def _page_header(doc: dict, page_no: int, total_pages: int) -> list[str]:
    """Build the ``════`` page-header block."""
    meta = doc.get("metadata") or {}
    date_range = meta.get("date_range") or {}
    start = date_range.get("start") or "?"
    end = date_range.get("end") or "?"
    total = meta.get("total_messages", len(doc.get("messages", [])))
    sources = meta.get("sources_used", meta.get("screenshots_used", "?"))
    return [
        _RULE,
        "UNSCROLL TRANSCRIPT",
        f"Thread: {_thread_name(doc)}",
        f"Platform: {meta.get('platform') or 'Unknown'}",
        f"Date range: {start} - {end}",
        f"Messages: {total}   Sources: {sources}",
        f"Page {page_no} of {total_pages}",
        _RULE,
    ]


def _message_block(msg: dict) -> list[str]:
    """Render one message: ``H:MM AM/PM · NAME`` then indented body lines."""
    name = sender_name(msg).upper()
    ts = parse_ts(msg.get("timestamp"))
    head = f"{_wall_clock(ts)} · {name}" if ts else name

    lines = [head]
    for body in _body_lines(msg):
        lines.append(f"{_INDENT}{body}")
    return lines


def _body_lines(msg: dict) -> list[str]:
    """Body lines for the script format (media/unreadable/text)."""
    mtype = msg.get("type") or "text"
    if mtype != "text":
        return [media_placeholder(mtype, msg.get("content"))]
    return body_lines(msg)


def _gap_marker(prev: datetime, current: datetime) -> str | None:
    """Return a silence marker for the gap, or ``None`` when below threshold.

    A gap of 7+ days is expressed by the following date section header alone,
    so no explicit marker is emitted for it.
    """
    delta = current - prev
    if delta >= _GAP_NEW_SECTION:
        return None
    if delta >= _GAP_DAY:
        days = delta.days
        return _section(f"[{days}-DAY GAP]")
    if delta >= _GAP_HOUR:
        hours = int(delta.total_seconds() // 3600)
        return _section(f"[{hours}-HOUR GAP]")
    return None


def _date_header(ts: datetime) -> str:
    """``── MONTH DAY, YEAR ──`` section header for a date."""
    label = f"{_MONTHS[ts.month - 1]} {ts.day}, {ts.year}"
    return _section(label)


def _section(label: str) -> str:
    """Format a ``── LABEL ──…`` divider line padded to the rule width."""
    prefix = f"{_DIVIDER * 2} {label} "
    pad = max(0, 52 - len(prefix))
    return prefix + _DIVIDER * pad


def _wall_clock(ts: datetime) -> str:
    """12-hour ``H:MM AM/PM`` clock (portable, no platform-specific strftime)."""
    hour24 = ts.hour
    suffix = "AM" if hour24 < 12 else "PM"
    hour12 = hour24 % 12 or 12
    return f"{hour12}:{ts.minute:02d} {suffix}"


def _thread_name(doc: dict) -> str:
    """Best-effort thread label: first non-self display name, else a default."""
    for msg in doc.get("messages", []):
        if msg.get("sender") == "other" and msg.get("display_name"):
            return str(msg["display_name"])
    return "Unknown Contact"
