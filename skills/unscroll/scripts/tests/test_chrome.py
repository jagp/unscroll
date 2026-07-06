"""Self-contained unit tests for core.chrome (no dependency on other modules)."""

import os
import sys
import unittest
from dataclasses import dataclass

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.chrome import (  # noqa: E402
    ContentSlice,
    content_slices,
    structural_content_slice,
    temporal_chrome_mask,
)

TOP = 15       # first content row
BOTTOM = 80    # first bottom-chrome row (content is [TOP, BOTTOM))
H, W = 100, 40


@dataclass
class _StubFrame:
    """Lightweight stand-in exposing the attributes content_slices requires."""

    gray: np.ndarray
    height: int
    width: int


def _make_stack(n: int, seed: int = 0) -> list[np.ndarray]:
    """Build n same-resolution luma frames: constant chrome bands, scrolling middle."""
    rng = np.random.default_rng(seed)
    frames = []
    for _ in range(n):
        g = np.empty((H, W), dtype=np.float32)
        g[:TOP, :] = 200.0                      # top chrome, identical across frames
        g[BOTTOM:, :] = 50.0                     # bottom chrome, identical across frames
        g[TOP:BOTTOM, :] = rng.uniform(0, 255, size=(BOTTOM - TOP, W))  # scrolls
        frames.append(g)
    return frames


class TestTemporalChromeMask(unittest.TestCase):
    def test_finds_content_bounds(self):
        slc = temporal_chrome_mask(_make_stack(5))
        self.assertEqual(slc.method, "temporal")
        self.assertLessEqual(abs(slc.top - TOP), 2)
        self.assertLessEqual(abs(slc.bottom - BOTTOM), 2)
        self.assertEqual(slc.confidence, "high")

    def test_all_identical_low_confidence(self):
        frame = np.full((H, W), 128.0, dtype=np.float32)
        slc = temporal_chrome_mask([frame.copy() for _ in range(4)])
        self.assertEqual(slc.confidence, "low")


class TestStructuralContentSlice(unittest.TestCase):
    def test_finds_constant_bands(self):
        frame = _make_stack(1, seed=7)[0]
        slc = structural_content_slice(frame)
        self.assertEqual(slc.method, "structural")
        self.assertLessEqual(abs(slc.top - TOP), 2)
        self.assertLessEqual(abs(slc.bottom - BOTTOM), 2)
        # The detected chrome bands must be near-constant color.
        self.assertLess(frame[: slc.top, :].var(), 1.0)
        self.assertLess(frame[slc.bottom :, :].var(), 1.0)


class TestContentSlicesOrchestrator(unittest.TestCase):
    def test_temporal_for_large_group(self):
        grays = _make_stack(4)
        frames = [_StubFrame(g, H, W) for g in grays]
        slices = content_slices(frames)
        self.assertEqual(len(slices), len(frames))
        for slc in slices:
            self.assertIsInstance(slc, ContentSlice)
            self.assertEqual(slc.method, "temporal")
            self.assertLessEqual(abs(slc.top - TOP), 2)
            self.assertLessEqual(abs(slc.bottom - BOTTOM), 2)

    def test_structural_for_small_group_and_order(self):
        big = _make_stack(3, seed=1)                      # (H, W)   -> temporal
        odd = np.full((H, W + 5), 100.0, dtype=np.float32)  # unique resolution -> structural
        odd[TOP:BOTTOM, :] = np.random.default_rng(2).uniform(0, 255, (BOTTOM - TOP, W + 5))
        frames = [
            _StubFrame(big[0], H, W),
            _StubFrame(odd, H, W + 5),
            _StubFrame(big[1], H, W),
            _StubFrame(big[2], H, W),
        ]
        slices = content_slices(frames)
        self.assertEqual(len(slices), 4)
        # Order preserved: index 1 is the lone odd-resolution frame -> structural.
        self.assertEqual(slices[1].method, "structural")
        self.assertEqual(slices[0].method, "temporal")
        self.assertEqual(slices[3].method, "temporal")


if __name__ == "__main__":
    unittest.main()
