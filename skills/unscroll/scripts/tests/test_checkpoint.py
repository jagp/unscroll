"""Unit tests for core.checkpoint (resumable pipeline manifest).

Run from the ``scripts`` directory::

    python -m unittest tests.test_checkpoint -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.checkpoint import Manifest, hash_inputs  # noqa: E402


class TestManifest(unittest.TestCase):
    def test_record_then_cached(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest.load(d)
            key = hash_inputs(["h1", "h2"], {"scale": 0.5} if False else 42)
            self.assertIsNone(m.cached("chrome", key))
            m.record("chrome", key, "out/chrome.png")
            self.assertEqual(m.cached("chrome", key), "out/chrome.png")

    def test_persistence_across_fresh_load(self):
        with tempfile.TemporaryDirectory() as d:
            key = hash_inputs("inputs", 7)
            Manifest.load(d).record("overlap", key, "out/overlap.json")
            # A brand-new Manifest reading the same workdir sees the record.
            reloaded = Manifest.load(d)
            self.assertEqual(reloaded.cached("overlap", key), "out/overlap.json")
            self.assertTrue(os.path.exists(reloaded.path))

    def test_unrecorded_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest.load(d)
            m.record("chrome", hash_inputs("a"), "ref")
            self.assertIsNone(m.cached("chrome", hash_inputs("b")))
            self.assertIsNone(m.cached("stitch", hash_inputs("a")))

    def test_fresh_load_no_file(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest.load(d)
            self.assertEqual(m.stages, {})
            self.assertFalse(os.path.exists(m.path))


class TestHashInputs(unittest.TestCase):
    def test_deterministic(self):
        a = hash_inputs(["x", 1], b"bytes", 99)
        b = hash_inputs(["x", 1], b"bytes", 99)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16)

    def test_input_sensitive(self):
        base = hash_inputs("stage", [1, 2, 3])
        self.assertNotEqual(base, hash_inputs("stage", [1, 2, 4]))
        self.assertNotEqual(base, hash_inputs("stage2", [1, 2, 3]))
        # Type-sensitive: int 1 differs from str "1".
        self.assertNotEqual(hash_inputs(1), hash_inputs("1"))
        # Framing-sensitive: no concatenation ambiguity.
        self.assertNotEqual(hash_inputs("a", "b"), hash_inputs("ab"))
        self.assertNotEqual(hash_inputs(["a", "b"]), hash_inputs(["ab"]))

    def test_accepts_mixed_types(self):
        # Should not raise on bytes / str / int / nested lists.
        h = hash_inputs(b"raw", "text", 5, [1, "two", [b"deep", 3]])
        self.assertEqual(len(h), 16)


if __name__ == "__main__":
    unittest.main()
