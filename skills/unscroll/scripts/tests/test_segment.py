"""Unit tests for core.segment — message segmentation + cross-frame dedup.

Uses the synthetic chat fixtures (``tests.make_fixtures``) which provide both a
tall chat image and per-bubble ground truth (location + sender), so segmentation
and de-duplication can be checked against known answers without real data.
"""

import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.chrome import ContentSlice  # noqa: E402
from core.imageio import Frame, content_hash, modal_color, to_gray  # noqa: E402
from core.offset import OverlapResult  # noqa: E402
from core.segment import Band, dedup_bands, segment_messages  # noqa: E402
from tests import make_fixtures  # noqa: E402


def _frame(rgb: np.ndarray, index: int = 0) -> Frame:
    """Wrap an RGB array in a minimal Frame (no disk I/O)."""
    return Frame(
        id=content_hash(rgb),
        path="",
        rgb=np.ascontiguousarray(rgb),
        gray=to_gray(rgb),
        width=int(rgb.shape[1]),
        height=int(rgb.shape[0]),
        source="screenshot",
        index=index,
        ts=None,
    )


def _full_slice(rgb: np.ndarray) -> ContentSlice:
    return ContentSlice(0, rgb.shape[0], "structural", "low")


def _chrome_slice(rgb: np.ndarray) -> ContentSlice:
    """Content slice using the fixtures' known chrome band heights."""
    top = make_fixtures._STATUS_H
    bottom = rgb.shape[0] - make_fixtures._INPUT_H
    return ContentSlice(top, bottom, "structural", "high")


class TestSegmentMessages(unittest.TestCase):
    def test_band_count_and_senders_match_truth(self):
        img, meta = make_fixtures.make_chat_with_meta(n_messages=20, seed=3)
        frame = _frame(img)
        bg = modal_color(img)
        bands = segment_messages(frame, _full_slice(img), bg)

        # Count within +/- 2 of ground truth.
        self.assertTrue(
            abs(len(bands) - len(meta)) <= 2,
            f"detected {len(bands)} bands, truth {len(meta)}",
        )

        # Every band is well-formed.
        for b in bands:
            self.assertIsInstance(b, Band)
            self.assertLess(b.top, b.bottom)
            x0, y0, x1, y1 = b.bbox
            self.assertLess(x0, x1)
            self.assertLess(y0, y1)
            self.assertIn(b.sender, ("self", "other", "unknown"))

        # Match each ground-truth bubble to the band covering its vertical center;
        # for clear cases the detected side must agree (unknown is tolerated).
        checked = 0
        correct = 0
        for m in meta:
            mid = (m.top + m.bottom) // 2
            hit = next((b for b in bands if b.top <= mid < b.bottom), None)
            if hit is None:
                continue
            checked += 1
            if hit.sender == m.sender:
                correct += 1
            else:
                # Only "unknown" is an acceptable disagreement, never a flip.
                self.assertEqual(
                    hit.sender, "unknown",
                    f"sender flip: got {hit.sender}, truth {m.sender}",
                )
        self.assertGreater(checked, 0)
        # The large majority of clear bubbles must be attributed correctly.
        self.assertGreaterEqual(correct, int(0.8 * checked))

    def test_empty_slice_returns_no_bands(self):
        img, _ = make_fixtures.make_chat_with_meta(n_messages=4, seed=0)
        frame = _frame(img)
        empty = ContentSlice(100, 100, "structural", "low")
        self.assertEqual(segment_messages(frame, empty, modal_color(img)), [])


class TestDedupBands(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="unscroll_test_crops_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dedup_collapses_overlap_duplicates(self):
        img, meta = make_fixtures.make_chat_with_meta(n_messages=24, seed=7)
        shots, overlaps = make_fixtures.slice_overlapping_with_truth(
            img, n_shots=5, overlap_frac=0.35, seed=7
        )

        frames = [_frame(s, index=i) for i, s in enumerate(shots)]
        slices = [_chrome_slice(s) for s in shots]
        order = list(range(len(frames)))

        bg = modal_color(img)
        bands_by_frame = {
            i: segment_messages(frames[i], slices[i], bg) for i in order
        }
        total_with_dupes = sum(len(v) for v in bands_by_frame.values())

        overlap_map = {}
        for k in range(len(frames) - 1):
            overlap_map[(k, k + 1)] = OverlapResult(
                overlap=int(overlaps[k]), score=1.0, psr=9.0, inlier=1.0,
                valid=True, confidence="high", per_row_corr=None,
            )

        emitted = dedup_bands(
            frames, order, slices, overlap_map, bands_by_frame,
            crops_dir=self.tmpdir,
        )

        # Dedup must remove duplicates (strictly fewer than the naive sum) and land
        # close to the true message total, not the inflated per-shot sum.
        self.assertLess(len(emitted), total_with_dupes)
        self.assertLessEqual(
            abs(len(emitted) - len(meta)), 3,
            f"emitted {len(emitted)} vs truth {len(meta)} "
            f"(sum-with-dupes {total_with_dupes})",
        )

        # Every emitted item is well-formed and has a real, non-empty crop.
        for item in emitted:
            self.assertIn(item["frame"], order)
            self.assertIsInstance(item["band"], Band)
            path = item["crop_png"]
            self.assertTrue(os.path.isfile(path), f"missing crop {path}")
            self.assertGreater(os.path.getsize(path), 0)

    def test_dedup_default_crops_dir(self):
        img, _ = make_fixtures.make_chat_with_meta(n_messages=6, seed=1)
        shots, overlaps = make_fixtures.slice_overlapping_with_truth(
            img, n_shots=2, overlap_frac=0.3, seed=1
        )
        frames = [_frame(s, index=i) for i, s in enumerate(shots)]
        slices = [_chrome_slice(s) for s in shots]
        bg = modal_color(img)
        bands_by_frame = {i: segment_messages(frames[i], slices[i], bg) for i in (0, 1)}
        overlap_map = {
            (0, 1): OverlapResult(int(overlaps[0]), 1.0, 9.0, 1.0, True, "high", None)
        }
        emitted = dedup_bands(frames, [0, 1], slices, overlap_map, bands_by_frame)
        try:
            self.assertTrue(emitted)
            crops_dir = os.path.dirname(emitted[0]["crop_png"])
            for item in emitted:
                self.assertTrue(os.path.isfile(item["crop_png"]))
        finally:
            if emitted:
                shutil.rmtree(os.path.dirname(emitted[0]["crop_png"]), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
