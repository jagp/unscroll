"""Flat plain-text transcript writer for the canonical unscroll document.

Renders one message per block: a ``HH:MM · NAME`` header line (time omitted when
the timestamp is missing) followed by the indented content, media placeholder, or
``[unreadable]`` marker. A small header carries thread/platform/date-range/count.
"""

from __future__ import annotations

from datetime import datetime

_INDENT = "  "


def write_text(doc: dict, path: str) -> None:
    """Write a flat plain-text transcript of ``doc`` to ``path`` (UTF-8)."""
    lines: list[str] = []
    lines.extend(_header(doc))
    lines.append("")

    for msg in doc.get("messages", []):
        lines.extend(_message_block(msg))
        lines.append("")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")


def _header(doc: dict) -> list[str]:
    """Build the plain-text header lines from document metadata."""
    meta = doc.get("metadata") or {}
    date_range = meta.get("date_range") or {}
    start = date_range.get("start") or "?"
    end = date_range.get("end") or "?"
    return [
        "UNSCROLL TRANSCRIPT",
        f"Thread: {_thread_name(doc)}",
        f"Platform: {meta.get('platform') or 'Unknown'}",
        f"Date range: {start} - {end}",
        f"Messages: {meta.get('total_messages', len(doc.get('messages', [])))}"
        f"   Sources: {meta.get('sources_used', meta.get('screenshots_used', '?'))}",
    ]


def _message_block(msg: dict) -> list[str]:
    """Render one message as a header line plus indented body lines."""
    name = sender_name(msg)
    time = _clock(msg.get("timestamp"))
    head = f"{time} · {name}" if time else name

    lines = [head]
    for body_line in body_lines(msg):
        lines.append(f"{_INDENT}{body_line}")
    return lines


def _thread_name(doc: dict) -> str:
    """Best-effort thread label: the first non-self display name, else a default."""
    for msg in doc.get("messages", []):
        if msg.get("sender") == "other" and msg.get("display_name"):
            return str(msg["display_name"])
    return "Unknown Contact"


def sender_name(msg: dict) -> str:
    """Human-readable sender label following the self/other/unknown rule."""
    sender = msg.get("sender")
    if sender == "self":
        return "You"
    if sender == "unknown":
        return "Unknown"
    return str(msg.get("display_name") or "Them")


def body_lines(msg: dict) -> list[str]:
    """Content lines for a message: text, media placeholder, or ``[unreadable]``."""
    mtype = msg.get("type") or "text"
    content = msg.get("content")

    if mtype != "text":
        return [media_placeholder(mtype, content)]
    if content is None:
        return ["[unreadable]"]
    return str(content).split("\n")


def media_placeholder(mtype: str, content: str | None) -> str:
    """Bracketed placeholder for a non-text message type."""
    if mtype == "image":
        return "[IMAGE]"
    if mtype == "audio":
        return "[AUDIO MESSAGE]"
    if mtype == "video":
        return "[VIDEO]"
    if mtype == "sticker":
        return "[STICKER]"
    if mtype == "reaction":
        return f"[REACTION: {content}]" if content else "[REACTION]"
    if mtype == "link_preview":
        return f"[LINK: {content}]" if content else "[LINK]"
    if mtype == "system":
        return str(content) if content else "[SYSTEM]"
    return f"[{mtype.upper()}]"


def _clock(timestamp: str | None) -> str | None:
    """Return ``HH:MM`` for an ISO timestamp, or ``None`` if unparseable/absent."""
    dt = parse_ts(timestamp)
    return dt.strftime("%H:%M") if dt else None


def parse_ts(timestamp: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp, tolerating a trailing ``Z``; ``None`` on failure."""
    if not timestamp:
        return None
    text = timestamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
