"""Unit tests for the unscroll output-format writers.

Run from the ``scripts`` directory:

    python -m unittest tests.test_formats -v
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from formats.json_out import validate_doc, write_json
from formats.markdown_out import write_markdown
from formats.text_out import write_text


def _message(index, sender, ts, content, mtype="text", **overrides):
    """Build a canonical message record with sensible defaults for tests."""
    msg = {
        "index": index,
        "sender": sender,
        "display_name": {"self": None, "other": "Jamie", "unknown": None}[sender],
        "timestamp": ts,
        "timestamp_source": "explicit" if ts else "absent",
        "content": content,
        "type": mtype,
        "confidence": "high",
        "notes": None,
    }
    msg.update(overrides)
    return msg


def _metadata(total, start, end, sources=3):
    """Build a canonical metadata block."""
    return {
        "skill": "unscroll",
        "platform": "iMessage",
        "platform_confidence": "high",
        "participants": 2,
        "date_range": {"start": start, "end": end},
        "total_messages": total,
        "sources_used": sources,
        "source_kind": "screenshots",
        "stitch_confidence": "high",
        "flags": [],
        "generated_at": "2026-07-04T12:00:00",
    }


def _small_doc():
    """A handful of messages spanning two months with a multi-day gap.

    Includes one null-timestamp message, one image message, and one null-content
    (unreadable) message.
    """
    messages = [
        _message(0, "other", "2020-06-14T14:47:00", "Hey, are you around?"),
        _message(1, "self", "2020-06-14T14:47:30", "Yeah what's up"),
        _message(2, "self", "2020-06-14T14:48:00", None, mtype="image"),
        _message(3, "other", "2020-06-14T14:49:00", None, confidence="low"),
        # Multi-day gap (>24h, <7d) into the same month.
        _message(4, "other", "2020-06-17T09:03:00", "Hey sorry about that"),
        # Month boundary -> forces a new page in markdown.
        _message(5, "self", "2020-07-02T10:00:00", "New month, new me"),
        # Null timestamp -> UNDATED section, must not crash.
        _message(6, "unknown", None, "who is this"),
    ]
    return {"metadata": _metadata(len(messages), "2020-06-14T14:47:00",
                                  "2020-07-02T10:00:00"), "messages": messages}


def _large_doc(count=160):
    """A dense doc of ``count`` messages with periodic natural gaps."""
    base = datetime(2021, 3, 1, 8, 0, 0)
    messages = []
    ts = base
    for i in range(count):
        # Every 10th message opens a multi-hour gap (a natural break point).
        step = timedelta(hours=6) if i and i % 10 == 0 else timedelta(minutes=2)
        ts = ts + step
        sender = "self" if i % 2 else "other"
        messages.append(_message(i, sender, ts.isoformat(timespec="seconds"),
                                 f"message number {i}"))
    return {"metadata": _metadata(count, base.isoformat(timespec="seconds"),
                                  ts.isoformat(timespec="seconds")), "messages": messages}


class JsonOutTests(unittest.TestCase):
    def test_round_trip_and_validation(self):
        doc = _small_doc()
        self.assertEqual(validate_doc(doc), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.json")
            write_json(doc, path)
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        self.assertEqual(loaded, doc)
        self.assertEqual(validate_doc(loaded), [])

    def test_validate_catches_bad_enum_and_index(self):
        doc = _small_doc()
        doc["messages"][1]["sender"] = "nobody"
        doc["messages"][2]["index"] = 1  # not monotonic (prev was 1)
        problems = validate_doc(doc)
        self.assertTrue(any("sender" in p for p in problems), problems)
        self.assertTrue(any("monotonic" in p for p in problems), problems)

    def test_validate_reports_missing_keys(self):
        doc = _small_doc()
        del doc["metadata"]["platform"]
        del doc["messages"][0]["confidence"]
        problems = validate_doc(doc)
        self.assertTrue(any("platform" in p for p in problems), problems)
        self.assertTrue(any("confidence" in p for p in problems), problems)


class TextOutTests(unittest.TestCase):
    def test_sender_names_and_placeholders(self):
        doc = _small_doc()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            write_text(doc, path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        self.assertIn("You", text)       # self sender
        self.assertIn("Jamie", text)     # other display_name
        self.assertIn("Unknown", text)   # unknown sender
        self.assertIn("[IMAGE]", text)   # image type placeholder
        self.assertIn("[unreadable]", text)  # null-content text message
        self.assertIn("14:47", text)     # HH:MM clock line

    def test_null_timestamp_line_has_no_clock(self):
        doc = _small_doc()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.txt")
            write_text(doc, path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        # The unknown/undated message renders its name with no time prefix.
        self.assertIn("Unknown\n  who is this", text)


class MarkdownOutTests(unittest.TestCase):
    def _render(self, doc):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.md")
            write_markdown(doc, path)
            with open(path, encoding="utf-8") as fh:
                return fh.read()

    def test_small_doc_headers_gaps_and_undated(self):
        text = self._render(_small_doc())
        self.assertIn("── JUNE 14, 2020 ", text)   # date section header
        self.assertIn("-DAY GAP]", text)           # multi-day gap marker
        self.assertIn("[IMAGE]", text)
        self.assertIn("[unreadable]", text)
        self.assertIn("UNDATED", text)             # null-timestamp section
        self.assertIn("Page 1 of", text)

    def test_multi_page_when_over_150_messages(self):
        text = self._render(_large_doc(160))
        self.assertIn("Page 1 of", text)
        # More than one page must be produced.
        page_headers = text.count("UNSCROLL TRANSCRIPT")
        self.assertGreater(page_headers, 1)
        self.assertIn("Page 2 of", text)

    def test_does_not_crash_on_only_null_timestamps(self):
        messages = [_message(0, "self", None, "hi"),
                    _message(1, "other", None, "there")]
        doc = {"metadata": _metadata(2, None, None), "messages": messages}
        text = self._render(doc)
        self.assertIn("UNDATED", text)


if __name__ == "__main__":
    unittest.main()
