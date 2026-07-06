"""Unit tests for core.stitch — validation-image assembly.

Builds a tall synthetic chat, slices overlapping shots, reconstructs order, and
stitches. Asserts the stitched height equals the unique-content height implied by
the detected slices/overlaps (``sum(content_h) - sum(overlaps)`` — the splice math
telescopes to this), and that a seam-straddling window reproduces a contiguous
block of the original content (no duplicated or dropped band).

Run:  cd skills/unscroll/scripts && python -m unittest tests.test_stitch -v
"""

from __future__ import annotations

import unittest

import numpy as np

from core.chrome import content_slices
from core.imageio import Frame, content_hash, to_gray
from core.order import build_overlap_matrix, order_frames
from core.stitch import compute_splices, stitch
from tests import make_fixtures
from tests.test_order import _rich_chat


def _frame_from_shot(shot: np.ndarray, index: int) -> Frame:
    rgb = np.ascontiguousarray(shot.astype(np.uint8))
    return Frame(
        id=content_hash(rgb),
        path=f"<shot-{index}>",
        rgb=rgb,
        gray=to_gray(rgb),
        width=int(rgb.shape[1]),
        height=int(rgb.shape[0]),
        source="screenshot",
        index=index,
        ts=float(index),
    )


def _pipeline(n_shots=4, overlap_frac=0.35, seed=2):
    tall = _rich_chat(seed)
    shots, _ = make_fixtures.slice_overlapping_with_truth(
        tall, n_shots=n_shots, overlap_frac=overlap_frac
    )
    frames = [_frame_from_shot(s, i) for i, s in enumerate(shots)]
    slices = content_slices(frames)
    matrix = build_overlap_matrix(frames, slices)
    result = order_frames(frames, slices, matrix, use_ts=True)
    splices = compute_splices(frames, result["order"], slices, matrix)
    return tall, frames, slices, result, matrix, splices


def _best_match_diff(window: np.ndarray, haystack: np.ndarray) -> tuple[int, float]:
    """Return (best_top, mean_abs_diff) sliding ``window`` over ``haystack`` rows."""
    wg = to_gray(window)
    hg = to_gray(haystack)
    wh = wg.shape[0]
    best_top, best = -1, float("inf")
    for t in range(0, hg.shape[0] - wh + 1):
        d = float(np.abs(hg[t : t + wh] - wg).mean())
        if d < best:
            best, best_top = d, t
    return best_top, best


class TestStitchHeight(unittest.TestCase):
    def test_height_equals_unique_content_height(self):
        _tall, frames, slices, result, matrix, splices = _pipeline()
        order = result["order"]
        self.assertEqual([g for g in result["gaps"] if g.get("fatal")], [])

        content_h = [slices[i].bottom - slices[i].top for i in order]
        overlaps = [matrix[(a, b)].overlap for a, b in zip(order, order[1:])]
        expected = sum(content_h) - sum(overlaps)

        img = stitch(frames, order, slices, splices, mark=True)
        self.assertEqual(img.shape[1], frames[order[0]].width)
        self.assertLessEqual(
            abs(img.shape[0] - expected),
            2,
            f"stitched height {img.shape[0]} vs expected {expected}",
        )


class TestStitchContinuity(unittest.TestCase):
    def test_seam_window_matches_contiguous_original_block(self):
        tall, frames, slices, result, _matrix, splices = _pipeline()
        order = result["order"]
        img = stitch(frames, order, slices, splices, mark=False)

        # Original content region (chrome stripped) — the ground-truth haystack.
        status_h = make_fixtures._STATUS_H
        input_h = make_fixtures._INPUT_H
        orig_content = tall[status_h : tall.shape[0] - input_h]

        # Locate the first seam row = height of the first contributed segment.
        first = order[0]
        s01 = splices[(first, order[1])]["splice"].s
        o01 = splices[(first, order[1])]["overlap"]
        seam = (slices[first].bottom - slices[first].top) - o01 + s01

        half = 30
        top = max(0, seam - half)
        bot = min(img.shape[0], seam + half)
        window = img[top:bot]

        best_top, best_diff = _best_match_diff(window, orig_content)
        # A faithful seam reproduces a real contiguous block: near-zero best diff.
        self.assertLess(
            best_diff, 6.0, f"seam window diff {best_diff} @ orig row {best_top}"
        )
        # And the match must be a SHARP minimum (shifting off it degrades badly),
        # proving no smeared duplicate/drop band.
        wh = to_gray(window).shape[0]
        hg = to_gray(orig_content)
        if best_top + wh + 8 <= hg.shape[0]:
            off = float(np.abs(hg[best_top + 8 : best_top + 8 + wh] - to_gray(window)).mean())
            self.assertGreater(off, best_diff + 2.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
