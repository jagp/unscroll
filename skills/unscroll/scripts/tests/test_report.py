"""Unit tests for core.report (rollup, invariants, document assembly).

Run from the ``scripts`` directory::

    python -m unittest tests.test_report -v
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.report import assemble_document, invariants, rollup  # noqa: E402

_META_KEYS = {
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
}

_NOW = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)


class _FakeOverlap:
    """Minimal OverlapResult stand-in exposing a ``confidence`` attribute."""

    def __init__(self, confidence):
        self.confidence = confidence


def _messages():
    return [
        {"index": 0, "sender": "self", "timestamp": "2020-06-14T14:47:00", "content": "Hey"},
        {"index": 1, "sender": "other", "timestamp": "2020-06-14T14:48:00", "content": "Yeah?"},
        {"index": 2, "sender": "self", "timestamp": None, "content": "Later"},
        {"index": 3, "sender": "other", "timestamp": "2020-06-17T09:03:00", "content": "Sorry"},
    ]


class TestRollup(unittest.TestCase):
    def test_all_required_keys(self):
        meta = rollup({"order": [0, 1], "gaps": []}, [], [], _messages(), now=_NOW)
        self.assertEqual(set(meta.keys()), _META_KEYS)
        self.assertEqual(meta["skill"], "unscroll")
        self.assertEqual(meta["source_kind"], "screenshots")

    def test_total_messages_and_participants(self):
        msgs = _messages()
        meta = rollup({}, [], [], msgs, now=_NOW)
        self.assertEqual(meta["total_messages"], 4)
        self.assertEqual(meta["participants"], 2)  # self + other

    def test_date_range_from_timestamps(self):
        meta = rollup({}, [], [], _messages(), now=_NOW)
        self.assertEqual(meta["date_range"]["start"], "2020-06-14T14:47:00")
        self.assertEqual(meta["date_range"]["end"], "2020-06-17T09:03:00")

    def test_generated_at_injectable(self):
        meta = rollup({}, [], [], _messages(), now=_NOW)
        self.assertEqual(meta["generated_at"], "2026-07-04T12:00:00+00:00")

    def test_fatal_gap_surfaces_in_flags(self):
        order_result = {
            "order": [0, 1, 2],
            "gaps": [{"i": 1, "j": 2, "fatal": True}],
        }
        meta = rollup(order_result, [], [], _messages(), now=_NOW)
        self.assertTrue(any(f.startswith("fatal_gap") for f in meta["flags"]))
        self.assertIn("fatal_gap:1->2", meta["flags"])

    def test_stitch_confidence_is_weakest(self):
        overlaps = [_FakeOverlap("high"), _FakeOverlap("low"), _FakeOverlap("medium")]
        meta = rollup({"confidence": "high"}, overlaps, [], _messages(), now=_NOW)
        self.assertEqual(meta["stitch_confidence"], "low")

    def test_platform_hint(self):
        meta = rollup({}, [], [], _messages(), platform_hint="iMessage", now=_NOW)
        self.assertEqual(meta["platform"], "iMessage")
        self.assertEqual(meta["platform_confidence"], "high")

    def test_empty_inputs_never_crash(self):
        meta = rollup(None, None, None, [], now=_NOW)
        self.assertEqual(meta["total_messages"], 0)
        self.assertEqual(meta["participants"], 2)
        self.assertIsNone(meta["date_range"]["start"])
        self.assertEqual(meta["stitch_confidence"], "low")
        self.assertEqual(meta["platform"], None)


class TestInvariants(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(invariants(30, [10, 20], _messages()), [])

    def test_stitch_height_mismatch(self):
        v = invariants(100, [10, 20], _messages())
        self.assertIn("stitch_height_mismatch", v)

    def test_duplicate_adjacent_content(self):
        msgs = [
            {"index": 0, "content": "same"},
            {"index": 1, "content": "same"},
        ]
        v = invariants(30, [10, 20], msgs)
        self.assertIn("duplicate_adjacent_content", v)

    def test_nonmonotonic_index(self):
        msgs = [
            {"index": 0, "content": "a"},
            {"index": 0, "content": "b"},
        ]
        v = invariants(30, [10, 20], msgs)
        self.assertIn("nonmonotonic_index", v)

    def test_null_content_not_duplicate(self):
        msgs = [
            {"index": 0, "content": None},
            {"index": 1, "content": None},
        ]
        self.assertEqual(invariants(30, [10, 20], msgs), [])


class TestAssembleDocument(unittest.TestCase):
    def test_shape(self):
        msgs = _messages()
        meta = rollup({}, [], [], msgs, now=_NOW)
        doc = assemble_document(msgs, meta)
        self.assertEqual(doc["metadata"], meta)
        self.assertEqual(doc["messages"], msgs)


if __name__ == "__main__":
    unittest.main()
