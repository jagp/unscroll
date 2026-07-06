"""Unit tests for core.offset — the vertical-overlap primitive.

Self-contained: builds a tall, structured luma "parent" image and slices two
overlapping views ``A`` and ``B`` from it so the TRUE overlap is known exactly,
then asserts recovery, validity gating, per-row robustness, and coarse-lag
behaviour.  No external fixtures, no third-party test deps.

Run:  cd skills/unscroll/scripts && python -m unittest tests.test_offset -v
"""

from __future__ import annotations

import unittest

import numpy as np

from core import offset
from core.offset import (
    OverlapResult,
    coarse_lag_fft,
    relaxed_vertical_offset,
    row_signature,
    vertical_offset,
)


def make_parent(height: int, width: int, seed: int = 1234) -> np.ndarray:
    """A tall structured luma image mimicking a chat screenshot.

    Two components, both needed by the primitive:
      * a distinctive per-row brightness (like alternating text / bubble-background
        rows) — this is the *vertical* structure that makes the overlap
        unambiguous and gives the collapsed 1-D profile a sharp correlation peak;
      * per-pixel horizontal texture — so each row has intra-row variation for the
        per-row ZNCC (a perfectly flat row would correlate to ~0).
    """
    rng = np.random.default_rng(seed)
    # Per-row brightness: white across rows -> razor-sharp vertical autocorrelation.
    per_row = rng.standard_normal(height).astype(np.float32)
    # Light 3-row smoothing for smooth vertical structure without broadening the
    # peak into an ambiguous random walk.
    per_row[1:-1] = (per_row[:-2] + per_row[1:-1] + per_row[2:]) / 3.0
    texture = rng.standard_normal((height, width)).astype(np.float32)
    img = per_row[:, None] + texture
    # Normalize to 0..255 luma.
    img -= img.min()
    img /= max(img.max(), 1e-6)
    return (img * 255.0).astype(np.float32)


class TestRowSignature(unittest.TestCase):
    def test_shape_and_dtype(self):
        img = make_parent(200, 300)
        sig = row_signature(img, ds=2, K=48)
        self.assertEqual(sig.shape, (100, 48))
        self.assertEqual(sig.dtype, np.float32)

    def test_ds_one_full_resolution(self):
        img = make_parent(120, 200)
        sig = row_signature(img, ds=1, K=32)
        self.assertEqual(sig.shape, (120, 32))

    def test_rgb_is_converted_to_luma(self):
        rgb = np.zeros((40, 60, 3), dtype=np.uint8)
        rgb[..., 1] = 200  # green channel
        sig = row_signature(rgb, ds=2, K=16)
        self.assertEqual(sig.shape, (20, 16))
        # 0.587 * 200 ~= 117.4 for a uniform green field.
        self.assertTrue(np.allclose(sig, 0.587 * 200, atol=1.0))

    def test_narrow_image_padded_to_K(self):
        img = make_parent(30, 10)  # width < K
        sig = row_signature(img, ds=1, K=48)
        self.assertEqual(sig.shape, (30, 48))


class TestVerticalOffsetRecovery(unittest.TestCase):
    def test_recovers_known_overlaps(self):
        parent = make_parent(1000, 320)
        h = 400
        for true_overlap in (24, 40, 80, 150, 220):
            a0 = 100
            A = parent[a0 : a0 + h]
            b0 = a0 + h - true_overlap
            B = parent[b0 : b0 + h]
            res = vertical_offset(A, B)
            self.assertTrue(
                res.valid,
                f"overlap={true_overlap}: expected valid, got {res}",
            )
            self.assertLessEqual(
                abs(res.overlap - true_overlap),
                2,
                f"overlap={true_overlap}: recovered {res.overlap} ({res})",
            )
            self.assertGreaterEqual(res.score, offset.ZNCC_MIN)
            self.assertGreaterEqual(res.psr, offset.PSR_MIN)
            self.assertGreaterEqual(res.inlier, offset.INLIER_MIN)
            self.assertIn(res.confidence, ("high", "medium", "low"))

    def test_per_row_corr_present_and_sized(self):
        parent = make_parent(800, 300)
        true_overlap = 60
        h = 300
        A = parent[50 : 50 + h]
        B = parent[50 + h - true_overlap : 50 + h - true_overlap + h]
        res = vertical_offset(A, B, ds=2)
        self.assertIsInstance(res, OverlapResult)
        self.assertIsNotNone(res.per_row_corr)
        # Documented deviation: one value per signature row -> overlap // ds.
        self.assertEqual(res.per_row_corr.shape[0], res.overlap // 2)
        self.assertEqual(res.per_row_corr.dtype, np.float32)


class TestValidityGating(unittest.TestCase):
    def test_unrelated_arrays_are_invalid(self):
        rng = np.random.default_rng(7)
        A = (rng.standard_normal((350, 320)) * 40 + 128).astype(np.float32)
        B = (rng.standard_normal((350, 320)) * 40 + 128).astype(np.float32)
        res = vertical_offset(A, B)
        self.assertFalse(res.valid, f"unrelated arrays should not validate: {res}")
        self.assertEqual(res.overlap, 0)
        self.assertEqual(res.confidence, "none")

    def test_two_different_parents_no_overlap(self):
        A = make_parent(400, 320, seed=1)
        B = make_parent(400, 320, seed=999)  # unrelated content
        res = vertical_offset(A, B)
        self.assertFalse(res.valid)
        self.assertEqual(res.overlap, 0)

    def test_min_overlap_respected(self):
        parent = make_parent(600, 300)
        # Genuine but tiny overlap below a large min_overlap floor -> rejected.
        true_overlap = 12
        h = 250
        A = parent[30 : 30 + h]
        B = parent[30 + h - true_overlap : 30 + h - true_overlap + h]
        res = vertical_offset(A, B, min_overlap=40)
        self.assertFalse(res.valid)


class TestPerRowRobustness(unittest.TestCase):
    def test_injected_status_bar_rows_survive_median(self):
        parent = make_parent(900, 320)
        true_overlap = 100
        h = 350
        A = parent[80 : 80 + h].copy()
        b0 = 80 + h - true_overlap
        B = parent[b0 : b0 + h].copy()
        # Inject a handful of differing "status bar" rows at the very top of B.
        rng = np.random.default_rng(3)
        B[0:6] = (rng.standard_normal((6, 320)) * 40 + 128).astype(np.float32)
        res = vertical_offset(A, B)
        self.assertTrue(res.valid, f"median should survive a few bad rows: {res}")
        self.assertLessEqual(abs(res.overlap - true_overlap), 4)
        # A few corrupted rows should not tank the robust median score.
        self.assertGreaterEqual(res.score, offset.ZNCC_MIN)


class TestCoarseLagFft(unittest.TestCase):
    def test_coarse_lag_matches_shift(self):
        parent = make_parent(1000, 320)
        h = 400
        for true_overlap in (40, 90, 160):
            A = parent[100 : 100 + h]
            B = parent[100 + h - true_overlap : 100 + h - true_overlap + h]
            ds = 2
            sigA = row_signature(A, ds=ds, K=48)
            sigB = row_signature(B, ds=ds, K=48)
            lag = coarse_lag_fft(sigA, sigB)  # signature-row units
            recovered = lag * ds
            self.assertLessEqual(
                abs(recovered - true_overlap),
                2 * ds,
                f"overlap={true_overlap}: coarse recovered {recovered} rows",
            )


class TestRelaxedLadder(unittest.TestCase):
    def test_ladder_levels_run_and_clamp(self):
        parent = make_parent(800, 300)
        true_overlap = 70
        h = 320
        A = parent[60 : 60 + h]
        B = parent[60 + h - true_overlap : 60 + h - true_overlap + h]
        for level in (0, 1, 2, 3, 99):  # 99 clamps to the last rung
            res = relaxed_vertical_offset(A, B, level)
            self.assertTrue(res.valid, f"level={level}: {res}")
            self.assertLessEqual(abs(res.overlap - true_overlap), 4)

    def test_ladder_full_resolution_rung_uses_ds_one(self):
        parent = make_parent(700, 300)
        true_overlap = 51  # odd -> ds=1 rung can hit it more precisely
        h = 300
        A = parent[40 : 40 + h]
        B = parent[40 + h - true_overlap : 40 + h - true_overlap + h]
        res = relaxed_vertical_offset(A, B, level=2)  # ds=1
        self.assertTrue(res.valid)
        self.assertLessEqual(abs(res.overlap - true_overlap), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
