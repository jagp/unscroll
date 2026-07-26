"""Unit tests for core.imageio, exercised with synthetic chat fixtures."""

from __future__ import annotations

import os
import tempfile
import unittest

import numpy as np

from core import imageio
from tests import make_fixtures


class TestImageIO(unittest.TestCase):
    """Cover the pixel-I/O primitives used across the pipeline."""

    def test_save_png_roundtrip(self) -> None:
        """A PNG written by save_png reloads to identical pixels."""
        rgb = make_fixtures.make_chat_image(n_messages=6, seed=3)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "chat.png")
            imageio.save_png(rgb, path)
            self.assertTrue(os.path.isfile(path))
            self.assertGreater(os.path.getsize(path), 0)
            back = imageio.load_image(path)
        self.assertEqual(back.shape, rgb.shape)
        self.assertEqual(back.dtype, np.uint8)
        np.testing.assert_array_equal(back, rgb)

    def test_to_gray_shape_dtype(self) -> None:
        """to_gray returns float32 (H, W) luma in range."""
        rgb = make_fixtures.make_chat_image(n_messages=4, seed=0)
        gray = imageio.to_gray(rgb)
        self.assertEqual(gray.shape, rgb.shape[:2])
        self.assertEqual(gray.dtype, np.float32)
        self.assertGreaterEqual(float(gray.min()), 0.0)
        self.assertLessEqual(float(gray.max()), 255.0)

    def test_to_gray_rec601(self) -> None:
        """A pure-red pixel maps to the Rec.601 red weight."""
        red = np.zeros((2, 2, 3), dtype=np.uint8)
        red[..., 0] = 255
        gray = imageio.to_gray(red)
        self.assertTrue(np.allclose(gray, 0.299 * 255, atol=1e-3))

    def test_content_hash_stable_and_length(self) -> None:
        """content_hash is 16 hex chars, deterministic, input-sensitive."""
        rgb = make_fixtures.make_chat_image(n_messages=5, seed=2)
        h1 = imageio.content_hash(rgb)
        h2 = imageio.content_hash(rgb.copy())
        self.assertEqual(len(h1), 16)
        self.assertEqual(h1, h2)
        self.assertTrue(all(c in "0123456789abcdef" for c in h1))
        # bytes input path
        self.assertEqual(len(imageio.content_hash(b"hello")), 16)
        # different content -> different hash
        other = rgb.copy()
        other[0, 0, 0] ^= 0xFF
        self.assertNotEqual(h1, imageio.content_hash(other))

    def test_modal_color_dominant_background(self) -> None:
        """modal_color recovers the dominant background of a synthetic image."""
        bg = np.array([200, 100, 50], dtype=np.uint8)
        img = np.empty((120, 90, 3), dtype=np.uint8)
        img[:] = bg
        # A minority foreground block must not sway the mode.
        img[10:30, 10:30] = (10, 240, 12)
        modal = imageio.modal_color(img)
        self.assertEqual(modal.shape, (3,))
        self.assertEqual(modal.dtype, np.uint8)
        # Each channel lands in the same 8-wide bin as the true background.
        self.assertTrue(np.all(np.abs(modal.astype(int) - bg.astype(int)) < 8))

    def test_modal_color_on_chat_fixture(self) -> None:
        """The chat fixture's mode is near-white background."""
        rgb = make_fixtures.make_chat_image(n_messages=10, seed=7)
        modal = imageio.modal_color(rgb)
        self.assertTrue(np.all(modal > 230))

    def test_make_frame_populates_fields(self) -> None:
        """make_frame fills every Frame field consistently."""
        rgb = make_fixtures.make_chat_image(n_messages=6, seed=4)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shot.png")
            imageio.save_png(rgb, path)
            frame = imageio.make_frame(path, source="screenshot", index=2)
        self.assertEqual(frame.path, path)
        self.assertEqual(frame.source, "screenshot")
        self.assertEqual(frame.index, 2)
        self.assertIsNone(frame.ts)
        self.assertEqual(frame.width, rgb.shape[1])
        self.assertEqual(frame.height, rgb.shape[0])
        self.assertEqual(frame.rgb.dtype, np.uint8)
        self.assertEqual(frame.gray.dtype, np.float32)
        self.assertEqual(frame.gray.shape, rgb.shape[:2])
        self.assertEqual(len(frame.id), 16)
        self.assertEqual(frame.id, imageio.content_hash(rgb))

    def test_make_frame_ts_passthrough(self) -> None:
        """An explicit timestamp is stored verbatim."""
        rgb = make_fixtures.make_chat_image(n_messages=3, seed=1)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shot.png")
            imageio.save_png(rgb, path)
            frame = imageio.make_frame(path, "video", 0, ts=1234.5)
        self.assertEqual(frame.ts, 1234.5)
        self.assertEqual(frame.source, "video")


class TestFixtures(unittest.TestCase):
    """Sanity-check the shared fixture generator itself."""

    def test_chat_meta_ordered_and_sided(self) -> None:
        """Bubble metadata is vertically ordered with valid senders."""
        _, meta = make_fixtures.make_chat_with_meta(n_messages=12, seed=5)
        self.assertEqual(len(meta), 12)
        for m in meta:
            self.assertIn(m.sender, ("self", "other"))
            self.assertLess(m.top, m.bottom)
        tops = [m.top for m in meta]
        self.assertEqual(tops, sorted(tops))

    def test_slice_overlap_truth(self) -> None:
        """Overlaps are reported and consistent with the requested fraction."""
        img = make_fixtures.make_chat_image(n_messages=20, seed=6)
        shots, overlaps = make_fixtures.slice_overlapping_with_truth(
            img, n_shots=5, overlap_frac=0.3
        )
        self.assertEqual(len(shots), 5)
        self.assertEqual(len(overlaps), 4)
        for ov in overlaps:
            self.assertGreater(ov, 0)
        # Every shot carries both chrome bands, so all share one width.
        self.assertEqual(len({s.shape[1] for s in shots}), 1)

    def test_slice_deterministic(self) -> None:
        """Same seed yields byte-identical slices."""
        img = make_fixtures.make_chat_image(n_messages=15, seed=8)
        a = make_fixtures.slice_overlapping(img, n_shots=4, jitter=2, seed=1)
        b = make_fixtures.slice_overlapping(img, n_shots=4, jitter=2, seed=1)
        for x, y in zip(a, b):
            np.testing.assert_array_equal(x, y)


if __name__ == "__main__":
    unittest.main()
