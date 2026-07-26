"""End-to-end integration tests for the unscroll orchestrator.

Drives the real :func:`unscroll.run_pipeline` on (a) a folder of overlapping
synthetic screenshots and (b) a synthesized scroll-capture video, asserting the
full chain produces valid, self-consistent artifacts. These exercise integration
across every module; per-module correctness is covered by the unit tests.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from core.imageio import save_png
from formats import json_out
from tests import make_fixtures

import unscroll


class TestScreenshotPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="unscroll_e2e_")
        cls.shots_dir = os.path.join(cls.tmp, "shots")
        os.makedirs(cls.shots_dir, exist_ok=True)
        img = make_fixtures.make_chat_image(n_messages=16, seed=3)
        shots, cls.truth_overlaps = make_fixtures.slice_overlapping_with_truth(
            img, n_shots=5, overlap_frac=0.35, seed=3,
        )
        # Save in true order with ascending mtimes (a realistic weak ordering prior).
        for i, shot in enumerate(shots):
            p = os.path.join(cls.shots_dir, f"IMG_{1000 + i}.png")
            save_png(shot, p)
            os.utime(p, (1_600_000_000 + i * 10, 1_600_000_000 + i * 10))
        cls.workdir = os.path.join(cls.tmp, "work")
        cls.report = unscroll.run_pipeline(
            cls.shots_dir, cls.workdir, now="2020-06-14T12:00:00+00:00",
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_intake_found_all_frames(self):
        self.assertEqual(self.report["frames"], 5)
        self.assertEqual(self.report["source_kind"], "screenshots")

    def test_validation_image_written(self):
        self.assertTrue(os.path.exists(self.report["validation_png"]))
        self.assertGreater(os.path.getsize(self.report["validation_png"]), 0)

    def test_canonical_json_is_valid(self):
        with open(self.report["transcript_json"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(json_out.validate_doc(doc), [])
        self.assertEqual(doc["metadata"]["source_kind"], "screenshots")
        self.assertGreater(doc["metadata"]["total_messages"], 0)

    def test_one_crop_per_message(self):
        with open(self.report["transcript_json"], encoding="utf-8") as fh:
            doc = json.load(fh)
        n = doc["metadata"]["total_messages"]
        crops = [f for f in os.listdir(self.report["crops_dir"])
                 if f.lower().endswith(".png")]
        self.assertEqual(len(crops), n)
        # Every message's notes points at an existing crop file.
        for m in doc["messages"]:
            self.assertTrue(m["notes"].startswith("crop:"))
            self.assertTrue(os.path.exists(m["notes"][len("crop:"):]))

    def test_no_fatal_gaps_on_contiguous_capture(self):
        # A genuinely overlapping capture must bridge without fatal gaps.
        self.assertEqual(self.report["fatal_gaps"], [])

    def test_reports_confidence(self):
        self.assertIn(self.report["confidence"], {"high", "medium", "low"})

    def test_ordered_capture_uses_chain_strategy(self):
        # In-order filenames must be confirmed by the O(n) chain probe alone,
        # never by paying for the full pairwise matrix.
        self.assertEqual(self.report["ordering_strategy"], "chain")

    def test_model_fill_then_render(self):
        # Simulate the model reading crops and filling text, then re-rendering.
        with open(self.report["transcript_json"], encoding="utf-8") as fh:
            doc = json.load(fh)
        for m in doc["messages"]:
            m["content"] = f"message {m['index']}"
            m["type"] = "text"  # the model corrects the puntable kind guess on read
            m["confidence"] = "high"
        filled = os.path.join(self.workdir, "filled.json")
        with open(filled, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        written = unscroll.render_document(
            doc, self.workdir, ["json", "text", "markdown"],
        )
        for fmt in ("json", "text", "markdown"):
            self.assertTrue(os.path.exists(written[fmt]))
        with open(written["text"], encoding="utf-8") as fh:
            txt = fh.read()
        self.assertIn("message 0", txt)
        with open(written["markdown"], encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("message 0", md)


class TestVideoPipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="unscroll_e2e_vid_")
        img = make_fixtures.make_chat_image(n_messages=14, seed=5)
        cls.video = os.path.join(cls.tmp, "scroll.mp4")
        make_fixtures.make_scroll_video(img, cls.video, scroll_px_per_frame=14)
        cls.workdir = os.path.join(cls.tmp, "work")
        cls.report = unscroll.run_pipeline(cls.video, cls.workdir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_video_intake_produced_frames(self):
        self.assertEqual(self.report["source_kind"], "video")
        self.assertGreaterEqual(self.report["frames"], 2)

    def test_video_outputs_valid_json(self):
        with open(self.report["transcript_json"], encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertEqual(json_out.validate_doc(doc), [])
        self.assertEqual(doc["metadata"]["source_kind"], "video")

    def test_video_validation_image(self):
        self.assertTrue(os.path.exists(self.report["validation_png"]))


if __name__ == "__main__":
    unittest.main()
