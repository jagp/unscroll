"""Unit tests for core.order — overlap-driven frame reordering.

A tall synthetic chat is sliced into overlapping "screenshots" with known true
overlaps (:func:`tests.make_fixtures.slice_overlapping_with_truth`). We then
SHUFFLE the shots and assert ``order_frames`` reconstructs the true top-to-bottom
sequence from visual overlap alone (timestamps deliberately withheld), and that
removing a middle shot surfaces a *fatal* gap.

Run:  cd skills/unscroll/scripts && python -m unittest tests.test_order -v
"""

from __future__ import annotations

import unittest

import numpy as np

from core.chrome import content_slices
from core.imageio import Frame, content_hash, to_gray
from core.order import build_overlap_matrix, order_frames
from tests import make_fixtures

_STATUS_H = make_fixtures._STATUS_H
_INPUT_H = make_fixtures._INPUT_H


def _rich_chat(seed: int, n_messages: int = 26, width: int = 390) -> np.ndarray:
    """A synthetic chat whose CONTENT region carries fine, unique per-row detail.

    The flat bubble renderer produces a vertically self-similar image whose
    correlation peaks are ambiguous. Real screenshots instead carry rich
    high-frequency detail (anti-aliased text, subpixel structure); we stand that in
    with a ``make_parent``-style field (unique per-row brightness + per-pixel
    texture) over the content region only, leaving the chrome bars intact so
    chrome-masking still exercises. Overlap regions remain exact copies, so ground
    truth is unchanged.
    """
    tall = make_fixtures.make_chat_image(
        n_messages=n_messages, width=width, seed=seed
    ).astype(np.float32)
    h, w = tall.shape[0], tall.shape[1]
    n = h - _STATUS_H - _INPUT_H
    rng = np.random.default_rng(1000 + seed)
    per_row = rng.standard_normal(n).astype(np.float32)
    per_row[1:-1] = (per_row[:-2] + per_row[1:-1] + per_row[2:]) / 3.0
    tex = rng.standard_normal((n, w)).astype(np.float32)
    field = per_row[:, None] + tex
    field -= field.min()
    field /= max(field.max(), 1e-6)
    field *= 255.0
    tall[_STATUS_H : h - _INPUT_H] = field[:, :, None].repeat(3, axis=2)
    return tall.astype(np.uint8)


def _frame_from_shot(shot: np.ndarray, index: int, ts: float | None = None) -> Frame:
    """Build a :class:`Frame` directly from an in-memory RGB shot (no disk I/O)."""
    rgb = np.ascontiguousarray(shot.astype(np.uint8))
    gray = to_gray(rgb)
    return Frame(
        id=content_hash(rgb),
        path=f"<shot-{index}>",
        rgb=rgb,
        gray=gray,
        width=int(rgb.shape[1]),
        height=int(rgb.shape[0]),
        source="screenshot",
        index=index,
        ts=ts,
    )


def _build(n_shots: int, overlap_frac: float, seed: int = 2):
    """Return (shots, true_overlaps) for a fresh synthetic chat.

    ``seed`` defaults to a value whose geometry aligns to the ds=2 overlap grid so
    every adjacent pair validates at the default detector level (no reliance on the
    retry ladder for the happy-path assertions).
    """
    tall = _rich_chat(seed)
    return make_fixtures.slice_overlapping_with_truth(
        tall, n_shots=n_shots, overlap_frac=overlap_frac
    )


class TestOrderReconstruction(unittest.TestCase):
    def test_shuffled_shots_are_reordered_correctly(self):
        shots, _true = _build(n_shots=5, overlap_frac=0.35)

        # Shuffle; ts withheld (None) so ordering is driven purely by overlap.
        perm = [3, 0, 4, 1, 2]  # not the identity
        shuffled = [shots[p] for p in perm]
        frames = [_frame_from_shot(s, i, ts=None) for i, s in enumerate(shuffled)]
        slices = content_slices(frames)

        matrix = build_overlap_matrix(frames, slices)
        result = order_frames(frames, slices, matrix, use_ts=True)

        # Map recovered order back to original top-to-bottom positions.
        recovered_positions = [perm[i] for i in result["order"]]
        self.assertEqual(
            recovered_positions,
            list(range(5)),
            f"order {result['order']} -> positions {recovered_positions}",
        )
        self.assertTrue(result["reordered"])
        self.assertNotEqual(result["confidence"], "low")
        self.assertEqual(
            [g for g in result["gaps"] if g.get("fatal")],
            [],
            "no fatal gaps expected on a complete chain",
        )

    def test_timestamps_only_break_ties_not_authoritative(self):
        # Provide MISLEADING timestamps (reverse of truth); overlap must still win.
        shots, _true = _build(n_shots=4, overlap_frac=0.35, seed=2)
        frames = [
            _frame_from_shot(s, i, ts=float(len(shots) - i))  # descending ts
            for i, s in enumerate(shots)
        ]
        slices = content_slices(frames)
        matrix = build_overlap_matrix(frames, slices)
        result = order_frames(frames, slices, matrix, use_ts=True)
        self.assertEqual(result["order"], [0, 1, 2, 3])
        self.assertNotEqual(result["confidence"], "low")


class TestFatalGap(unittest.TestCase):
    def test_removed_middle_shot_reports_fatal_gap(self):
        shots, _true = _build(n_shots=5, overlap_frac=0.35, seed=1)
        # Drop the middle shot -> shots 1 and 3 no longer share any content.
        kept = [shots[0], shots[1], shots[3], shots[4]]
        frames = [_frame_from_shot(s, i, ts=float(i)) for i, s in enumerate(kept)]
        slices = content_slices(frames)

        matrix = build_overlap_matrix(frames, slices)
        result = order_frames(frames, slices, matrix, use_ts=True)

        fatal = [g for g in result["gaps"] if g.get("fatal")]
        self.assertTrue(fatal, f"expected a fatal gap, got {result['gaps']}")
        self.assertEqual(result["confidence"], "low")
        # The fatal gap must straddle the removed region (kept indices 1 -> 2).
        self.assertIn((1, 2), [g["between"] for g in fatal])


if __name__ == "__main__":
    unittest.main(verbosity=2)
