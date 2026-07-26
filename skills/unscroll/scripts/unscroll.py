#!/usr/bin/env python3
# Keep the module docstring ASCII: argparse prints it as the --help description,
# and a Windows cp1252 console raises UnicodeEncodeError on anything outside it.
"""unscroll - reconstruct an async chat thread into a structured transcript.

This is the pipeline ORCHESTRATOR. Deterministic geometry (numpy/Pillow/ffmpeg)
does everything except read glyphs; the multimodal model reads the per-message
crops this script emits and fills in the text. See ``CONTRACT.md``.

Two entry points cover the whole flow:

* ``run <input> --workdir <wd>`` - intake (a folder of screenshots, a .zip of
  them, or a scroll-capture video) -> chrome-mask -> overlap -> order -> stitch
  -> segment/dedup. Emits, into ``<wd>``:
    - ``validation.png``       the stitched thread (a completeness check)
    - ``crops/msg_*.png``      one high-res crop per message, in order
    - ``transcript.json``      the canonical document (content=null until read)
    - ``report.json``          confidence, flags, per-adjacency detail
    - ``transcript.txt`` / ``transcript.md``  draft renders (content=[unreadable])
  The model then reads each crop (path carried in each message's ``notes``),
  fills ``content`` / ``display_name`` / visible ``timestamp`` / corrects
  ``type``, and re-renders with ``render``.
* ``render <doc.json> --workdir <wd> [--formats json,text,markdown]`` - write the
  final transcripts from a completed canonical document.

Puntable by design: media, timestamps and reactions that cannot be read become
``null`` + ``low`` confidence + a note; only an unrecoverable overlap gap (after
the retry ladder) is fatal, and even then the partial result is still written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import zipfile

# Make ``core`` / ``formats`` importable whether run as a script or module.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import numpy as np  # noqa: E402

from core import order as order_mod  # noqa: E402
from core import report as report_mod  # noqa: E402
from core import segment as segment_mod  # noqa: E402
from core import stitch as stitch_mod  # noqa: E402
from core.chrome import content_slices  # noqa: E402
from core.checkpoint import Manifest, hash_inputs  # noqa: E402
from core.imageio import make_frame, modal_color, save_png  # noqa: E402
from core.video import video_to_frames  # noqa: E402
from formats import json_out, markdown_out, text_out  # noqa: E402

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".heic", ".heif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
# Canonical message ``type`` enum (CONTRACT.md). Segment's cheap kind guess uses a
# couple of values outside it ("unknown"); coerce those to a schema-valid default.
_TYPE_ENUM = {"text", "image", "audio", "video", "sticker", "reaction",
              "link_preview", "system"}


# --------------------------------------------------------------------------- #
# intake
# --------------------------------------------------------------------------- #
def _list_images(folder: str) -> list[str]:
    """Return image file paths in ``folder`` sorted by (mtime, name)."""
    paths = [
        os.path.join(folder, n)
        for n in os.listdir(folder)
        if os.path.splitext(n)[1].lower() in _IMAGE_EXTS
    ]

    def _key(p: str):
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0.0
        return (mtime, os.path.basename(p))

    return sorted(paths, key=_key)


def intake(input_path: str, workdir: str) -> tuple[list, str]:
    """Turn ``input_path`` into a list of Frames plus the resolved source kind.

    Accepts a folder of screenshots, a ``.zip`` of screenshots, a single image,
    or a scroll-capture video. File names are opaque; order is a weak prior only
    (overlap-based ordering corrects it later).
    """
    os.makedirs(workdir, exist_ok=True)
    ext = os.path.splitext(input_path)[1].lower()

    if os.path.isdir(input_path):
        image_paths, source_kind = _list_images(input_path), "screenshots"
    elif ext == ".zip":
        extract_dir = os.path.join(workdir, "unzipped")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(input_path) as zf:
            zf.extractall(extract_dir)
        # Images may sit in a nested folder; walk the whole tree.
        found: list[str] = []
        for root, _dirs, names in os.walk(extract_dir):
            for n in names:
                if os.path.splitext(n)[1].lower() in _IMAGE_EXTS:
                    found.append(os.path.join(root, n))
        image_paths, source_kind = sorted(found), "screenshots"
    elif ext in _VIDEO_EXTS:
        frames = video_to_frames(input_path, os.path.join(workdir, "video"))
        return frames, "video"
    elif ext in _IMAGE_EXTS:
        image_paths, source_kind = [input_path], "screenshots"
    else:
        raise ValueError(f"Unrecognized input: {input_path!r}")

    frames = []
    for idx, path in enumerate(image_paths):
        try:
            frames.append(make_frame(path, source="screenshot", index=idx,
                                     ts=os.path.getmtime(path)))
        except Exception as exc:  # a corrupt file must not sink the batch
            print(f"warning: skipping unreadable image {path}: {exc}",
                  file=sys.stderr)
    return frames, source_kind


# --------------------------------------------------------------------------- #
# message assembly
# --------------------------------------------------------------------------- #
def _coerce_type(kind: str) -> str:
    """Map segment's cheap ``kind`` guess onto the canonical type enum."""
    return kind if kind in _TYPE_ENUM else "text"


def _partial_messages(dedup_items: list[dict]) -> list[dict]:
    """Build canonical message records (content unread) from dedup output.

    Each message points at its crop via ``notes`` so the model knows which image
    to read. Everything the model must fill starts null/low-confidence.
    """
    messages: list[dict] = []
    for i, item in enumerate(dedup_items):
        band = item["band"]
        messages.append({
            "index": i,
            "sender": getattr(band, "sender", "unknown"),
            "display_name": None,
            "timestamp": None,
            "timestamp_source": "absent",
            "content": None,
            "type": _coerce_type(getattr(band, "kind", "text")),
            "confidence": "low",
            "notes": f"crop:{item.get('crop_png', '')}",
        })
    return messages


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_document(doc: dict, workdir: str, formats: list[str]) -> dict[str, str]:
    """Write requested formats from a canonical document; return {fmt: path}."""
    os.makedirs(workdir, exist_ok=True)
    written: dict[str, str] = {}
    if "json" in formats:
        p = os.path.join(workdir, "transcript.json")
        json_out.write_json(doc, p)
        written["json"] = p
    if "text" in formats:
        p = os.path.join(workdir, "transcript.txt")
        text_out.write_text(doc, p)
        written["text"] = p
    if "markdown" in formats:
        p = os.path.join(workdir, "transcript.md")
        markdown_out.write_markdown(doc, p)
        written["markdown"] = p
    return written


# --------------------------------------------------------------------------- #
# the pipeline
# --------------------------------------------------------------------------- #
def run_pipeline(
    input_path: str,
    workdir: str,
    mark: bool = True,
    use_ts: bool = True,
    platform_hint: str | None = None,
    formats: tuple[str, ...] = ("json", "text", "markdown"),
    now=None,
) -> dict:
    """Run intake→chrome→overlap→order→stitch→segment and write all artifacts.

    Returns a summary dict (paths, counts, confidence, flags, fatal-gap list). The
    model is expected to read the emitted crops, fill message text, and call
    :func:`render_document` again. Never raises on a fatal overlap gap — it records
    it and still writes the partial result.
    """
    os.makedirs(workdir, exist_ok=True)
    manifest = Manifest.load(workdir)

    frames, source_kind = intake(input_path, workdir)
    if not frames:
        raise ValueError("No usable images or frames found in input.")

    # --- chrome mask (registry-free) ---
    slices = content_slices(frames)

    # --- overlap + ordering: chain-first (O(n) consecutive pairs; the full
    #     pairwise matrix is computed only when the capture order can't be
    #     confirmed — retry ladder + fatal-gap detection inside) ---
    frame_key = hash_inputs([f.id for f in frames])
    order_result = order_mod.order_frames_chain_first(frames, slices, use_ts=use_ts)
    manifest.record("overlap", frame_key, "in-memory")
    order = order_result["order"]
    matrix = order_result["matrix"]
    fatal_gaps = [g for g in order_result["gaps"] if g.get("fatal")]

    # --- stitch validation image ---
    splices = stitch_mod.compute_splices(frames, order, slices, matrix)
    stitched = stitch_mod.stitch(frames, order, slices, splices, mark=mark)
    validation_png = os.path.join(workdir, "validation.png")
    if stitched.size:
        save_png(stitched, validation_png)

    # --- segment + cross-overlap dedup ---
    bands_by_frame = {
        i: segment_mod.segment_messages(frames[i], slices[i],
                                        modal_color(frames[i].rgb))
        for i in range(len(frames))
    }
    crops_dir = os.path.join(workdir, "crops")
    dedup_items = segment_mod.dedup_bands(
        frames, order, slices, matrix, bands_by_frame, crops_dir=crops_dir,
    )
    messages = _partial_messages(dedup_items)

    # --- metadata + invariants ---
    # Contributed segment heights (post-splice) — their sum equals the stitched
    # height when the stitch is uncorrupted; computed by the same code stitch uses.
    seg_heights = stitch_mod.segment_heights(frames, order, slices, splices)
    violations = report_mod.invariants(
        int(stitched.shape[0]) if stitched.size else 0, seg_heights, messages,
    )
    metadata = report_mod.rollup(
        order_result, matrix, splices, messages,
        source_kind=source_kind, platform_hint=platform_hint, now=now,
    )
    for v in violations:
        if v not in metadata["flags"]:
            metadata["flags"].append(v)
    metadata["sources_used"] = len(frames)

    doc = report_mod.assemble_document(messages, metadata)

    # --- write everything ---
    written = render_document(doc, workdir, list(formats))
    report_path = os.path.join(workdir, "report.json")
    report = {
        "input": input_path,
        "source_kind": source_kind,
        "frames": len(frames),
        "order": order,
        "reordered": order_result["reordered"],
        "ordering_strategy": order_result.get("strategy", "matrix"),
        "confidence": order_result["confidence"],
        "total_messages": len(messages),
        "fatal_gaps": fatal_gaps,
        "flags": metadata["flags"],
        "validation_png": validation_png if stitched.size else None,
        "crops_dir": crops_dir,
        "outputs": written,
    }
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    manifest.save()

    report["report_json"] = report_path
    report["transcript_json"] = written.get("json")
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cmd_run(args: argparse.Namespace) -> int:
    workdir = args.workdir or tempfile.mkdtemp(prefix="unscroll_")
    report = run_pipeline(
        args.input, workdir,
        mark=not args.no_mark, use_ts=not args.no_timestamps,
        platform_hint=args.platform,
    )
    print(json.dumps({
        "workdir": workdir,
        "source_kind": report["source_kind"],
        "frames": report["frames"],
        "total_messages": report["total_messages"],
        "confidence": report["confidence"],
        "reordered": report["reordered"],
        "fatal_gaps": len(report["fatal_gaps"]),
        "flags": report["flags"],
        "validation_png": report["validation_png"],
        "crops_dir": report["crops_dir"],
        "transcript_json": report["transcript_json"],
        "report_json": report["report_json"],
    }, indent=2))
    if report["fatal_gaps"]:
        print(
            "\nFATAL: one or more overlap gaps could not be bridged. The capture "
            "is missing a section between the reported frames. Partial output was "
            "still written; re-capture the missing overlap and re-run.",
            file=sys.stderr,
        )
        return 2
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    with open(args.doc, encoding="utf-8") as fh:
        doc = json.load(fh)
    problems = json_out.validate_doc(doc)
    if problems:
        print("Document schema problems:\n  " + "\n  ".join(problems),
              file=sys.stderr)
        return 1
    workdir = args.workdir or os.path.dirname(os.path.abspath(args.doc))
    written = render_document(doc, workdir, args.formats.split(","))
    print(json.dumps(written, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unscroll", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="run the full reconstruction pipeline")
    p_run.add_argument("input", help="folder of screenshots, a .zip, an image, or a video")
    p_run.add_argument("--workdir", help="work/output directory (default: temp)")
    p_run.add_argument("--platform", help="optional platform hint (e.g. 'iOS Messages')")
    p_run.add_argument("--no-mark", action="store_true", help="no splice hairlines")
    p_run.add_argument("--no-timestamps", action="store_true",
                       help="ignore file timestamps entirely when ordering")
    p_run.set_defaults(func=_cmd_run)

    p_render = sub.add_parser("render", help="render transcripts from a completed doc")
    p_render.add_argument("doc", help="path to a canonical transcript.json")
    p_render.add_argument("--workdir", help="output directory (default: doc's dir)")
    p_render.add_argument("--formats", default="json,text,markdown",
                          help="comma list of json,text,markdown")
    p_render.set_defaults(func=_cmd_render)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
