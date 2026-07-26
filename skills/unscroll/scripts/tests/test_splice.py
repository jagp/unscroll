"""Self-contained unit tests for core.splice (no dependency on other modules)."""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.splice import (  # noqa: E402
    SpliceResult,
    gutter_profile,
    select_splice_row,
)

H, W = 60, 50
BG = np.array([240, 240, 240], dtype=np.uint8)
GUTTERS = [(10, 16), (26, 32)]   # half-open gutter row spans


def _textured(rgb, r0, r1, seed):
    rng = np.random.default_rng(seed)
    rgb[r0:r1, :, :] = rng.integers(0, 200, size=(r1 - r0, W, 3), dtype=np.uint8)


def _make_content() -> np.ndarray:
    """Background image with two uniform gutter rows between textured bubble bands."""
    rgb = np.empty((H, W, 3), dtype=np.uint8)
    rgb[:, :, :] = BG
    _textured(rgb, 0, 10, 1)
    _textured(rgb, 16, 26, 2)
    _textured(rgb, 32, H, 3)
    return rgb


def _in_gutter(s: int) -> bool:
    return any(a <= s < b for a, b in GUTTERS)


class TestGutterProfile(unittest.TestCase):
    def test_edge_low_in_gutter_high_in_bubble(self):
        rgb = _make_content()
        bg_frac, edge = gutter_profile(rgb, BG)
        self.assertEqual(edge.shape, (H,))
        for a, b in GUTTERS:
            self.assertLess(float(edge[a:b].max()), 1.0)      # flat gutters
            self.assertGreater(float(bg_frac[a:b].min()), 0.99)
        self.assertGreater(float(edge[0:10].mean()), 10.0)    # textured bubble


class TestSelectSpliceRow(unittest.TestCase):
    def test_lands_in_gutter(self):
        rgb = _make_content()
        overlap = 40
        res = select_splice_row(rgb, overlap)
        self.assertIsInstance(res, SpliceResult)
        self.assertTrue(0 <= res.s < overlap)
        self.assertTrue(_in_gutter(res.s), f"s={res.s} not in a gutter")
        _, edge = gutter_profile(rgb, BG)
        self.assertLess(float(edge[res.s]), 1.0)              # cut sits on flat row
        self.assertIn(res.confidence, ("high", "medium"))
        self.assertGreaterEqual(res.band_width, 3)

    def test_respects_overlap_bound(self):
        rgb = _make_content()
        # Overlap that excludes the second gutter -> must pick the first, staying < overlap.
        overlap = 20
        res = select_splice_row(rgb, overlap)
        self.assertTrue(0 <= res.s < overlap)
        self.assertTrue(_in_gutter(res.s))

    def test_per_row_corr_accepted(self):
        rgb = _make_content()
        overlap = 40
        corr = np.full(overlap, 0.95, dtype=np.float32)
        res = select_splice_row(rgb, overlap, per_row_corr=corr)
        self.assertTrue(_in_gutter(res.s))
        self.assertEqual(res.confidence, "high")

    def test_never_fails_without_gutter(self):
        rng = np.random.default_rng(9)
        rgb = rng.integers(0, 200, size=(H, W, 3), dtype=np.uint8)  # all textured
        overlap = 40
        res = select_splice_row(rgb, overlap)
        self.assertTrue(0 <= res.s < overlap)
        self.assertEqual(res.confidence, "low")


if __name__ == "__main__":
    unittest.main()
