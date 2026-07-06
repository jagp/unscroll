"""JSON output writer and schema validator for the canonical unscroll document.

``json_out`` is the source of truth for the canonical document shape. ``write_json``
pretty-prints the document; ``validate_doc`` reports schema problems so the other
writers can assume (but need not require) a clean document.
"""

from __future__ import annotations

import json
from typing import Any

# Allowed enum values per CONTRACT.md "Canonical JSON schema".
_SENDERS = frozenset({"self", "other", "unknown"})
_TYPES = frozenset(
    {"text", "image", "audio", "video", "sticker", "reaction", "link_preview", "system"}
)
_CONFIDENCE = frozenset({"high", "medium", "low"})
_TS_SOURCE = frozenset({"explicit", "interpolated", "absent"})

_METADATA_KEYS = (
    "skill",
    "platform",
    "platform_confidence",
    "participants",
    "date_range",
    "total_messages",
    "sources_used",
    "source_kind",
    "stitch_confidence",
    "flags",
    "generated_at",
)
_MESSAGE_KEYS = (
    "index",
    "sender",
    "display_name",
    "timestamp",
    "timestamp_source",
    "content",
    "type",
    "confidence",
    "notes",
)


def write_json(doc: dict, path: str) -> None:
    """Pretty-print the canonical document to ``path`` (UTF-8, indent=2)."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def validate_doc(doc: dict) -> list[str]:
    """Return a list of schema problems in ``doc`` (empty list = valid)."""
    problems: list[str] = []

    if not isinstance(doc, dict):
        return ["document is not a dict"]

    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        problems.append("metadata: missing or not a dict")
    else:
        for key in _METADATA_KEYS:
            if key not in metadata:
                problems.append(f"metadata: missing key '{key}'")
        date_range = metadata.get("date_range")
        if date_range is not None and not isinstance(date_range, dict):
            problems.append("metadata.date_range: not a dict")
        flags = metadata.get("flags")
        if flags is not None and not isinstance(flags, list):
            problems.append("metadata.flags: not a list")

    messages = doc.get("messages")
    if not isinstance(messages, list):
        problems.append("messages: missing or not a list")
        return problems

    prev_index: int | None = None
    for i, msg in enumerate(messages):
        problems.extend(_validate_message(msg, i, prev_index))
        if isinstance(msg, dict) and isinstance(msg.get("index"), int):
            prev_index = msg["index"]

    return problems


def _validate_message(msg: Any, pos: int, prev_index: int | None) -> list[str]:
    """Validate a single message record at list position ``pos``."""
    problems: list[str] = []
    tag = f"messages[{pos}]"

    if not isinstance(msg, dict):
        return [f"{tag}: not a dict"]

    for key in _MESSAGE_KEYS:
        if key not in msg:
            problems.append(f"{tag}: missing key '{key}'")

    index = msg.get("index")
    if not isinstance(index, int):
        problems.append(f"{tag}.index: not an int")
    elif prev_index is not None and index <= prev_index:
        problems.append(f"{tag}.index: not monotonic (>{prev_index} required, got {index})")

    _check_enum(problems, tag, "sender", msg.get("sender"), _SENDERS)
    _check_enum(problems, tag, "type", msg.get("type"), _TYPES)
    _check_enum(problems, tag, "confidence", msg.get("confidence"), _CONFIDENCE)
    _check_enum(problems, tag, "timestamp_source", msg.get("timestamp_source"), _TS_SOURCE)

    content = msg.get("content")
    if content is not None and not isinstance(content, str):
        problems.append(f"{tag}.content: not a string or null")
    timestamp = msg.get("timestamp")
    if timestamp is not None and not isinstance(timestamp, str):
        problems.append(f"{tag}.timestamp: not a string or null")

    return problems


def _check_enum(
    problems: list[str], tag: str, field: str, value: Any, allowed: frozenset[str]
) -> None:
    """Append a problem if ``value`` is not one of ``allowed``."""
    if value not in allowed:
        problems.append(f"{tag}.{field}: '{value}' not in {sorted(allowed)}")
