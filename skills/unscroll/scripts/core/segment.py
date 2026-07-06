"""core/segment.py — message segmentation and cross-frame dedup (geometry only).

This module carves a content region into individual message *bands* and, across a
set of overlapping frames, emits each message once (de-duplicating the copies that
recur inside overlap zones).  It is pure GEOMETRY: it never reads glyph text.  Each
emitted message is saved as a high-resolution crop so a downstream multimodal model
can read the actual text later.

Coordinates (see CONTRACT.md): rows increase downward.  A :class:`Band` records
``top``/``bottom`` as row indices *within the frame* (not within the content slice),
and ``bbox`` as ``(x0, y0, x1, y1)`` of the foreground bubble mass, also in frame
coordinates.  ``bottom``/``y1``/``x1`` are exclusive.

Everything here is PUNTABLE: media / audio / unusual layouts never raise.  When a
heuristic is unsure it labels ``kind``/``sender`` ``"unknown"`` and moves on.

Standard library + numpy + Pillow only.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

import numpy as np

from core.imageio import Frame, save_png

__all__ = ["Band", "segment_messages", "dedup_bands"]

# --------------------------------------------------------------------------- #
# tuning constants
# --------------------------------------------------------------------------- #
_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)

_BG_TOL = 16.0            # per-channel tolerance for "this pixel matches bg"
_ROW_FG_FRAC = 0.02       # a row is foreground if > this fraction of it is non-bg
_MERGE_GAP = 6            # merge foreground runs separated by fewer bg rows than this
_MIN_BAND_H = 4           # discard foreground runs shorter than this (px, noise)

_SIDE_MARGIN = 0.06       # dead-band around center for left/right sender decision
_FULLWIDTH_FRAC = 0.90    # bubble mass spanning >= this fraction of width -> unknown

_IMG_MIN_H = 200          # a "large" band (px) that may be an image
_IMG_EDGE_MAX = 3.0       # mean fg edge energy below this reads as smooth (image-ish)
_IMG_FILL_MIN = 0.55      # image regions are densely filled
_AUDIO_MAX_H = 56         # audio waveforms sit in short bands

_SIG_GRID = 24            # downsampled signature side length for dedup correlation
_DEDUP_CORR = 0.85        # signature correlation above which two bands are duplicates
_DEDUP_VSPAN = 0.5        # min vertical-span overlap ratio for a dedup candidate


@dataclass
class Band:
    """One message region within a frame (all coords in frame space)."""

    top: int
    bottom: int                    # exclusive
    sender: str                    # "self" | "other" | "unknown"
    kind: str                      # text|image|audio|video|sticker|reaction|system|unknown
    bbox: tuple                    # (x0, y0, x1, y1) of foreground bubble mass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _foreground_mask(content_rgb: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """Boolean ``(H, W)`` mask of pixels that differ from ``bg`` beyond tolerance."""
    diff = np.abs(content_rgb.astype(np.float32) - bg.reshape(1, 1, 3))
    return np.any(diff > _BG_TOL, axis=2)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return ``(start, end)`` half-open spans of contiguous True runs in ``mask``."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(mask)))
    return spans


def _merge_and_filter(
    spans: list[tuple[int, int]], min_gap: int, min_h: int
) -> list[tuple[int, int]]:
    """Merge spans separated by < ``min_gap`` rows, then drop spans shorter than ``min_h``."""
    if not spans:
        return []
    merged = [spans[0]]
    for s, e in spans[1:]:
        ps, pe = merged[-1]
        if s - pe < min_gap:
            merged[-1] = (ps, e)
        else:
            merged.append((s, e))
    return [(s, e) for s, e in merged if e - s >= min_h]


def _guess_kind(band_rgb: np.ndarray, band_fg: np.ndarray) -> str:
    """Cheap shape heuristic for message ``kind``; defaults to ``"text"`` (puntable)."""
    h = band_fg.shape[0]
    fill = float(band_fg.mean())
    if fill <= 0.0:
        return "unknown"

    luma = band_rgb.astype(np.float32) @ _LUMA
    if luma.shape[1] >= 2:
        edges = np.abs(np.diff(luma, axis=1))
        both_fg = band_fg[:, :-1] & band_fg[:, 1:]
        edge_energy = float(edges[both_fg].mean()) if both_fg.any() else 0.0
    else:
        edge_energy = 0.0

    # Large, densely filled, smooth region -> most likely an inline image/sticker.
    # (A text bubble, however large, has high edge energy from the glyphs and is
    # excluded.) Audio is intentionally NOT guessed here: a cheap waveform
    # heuristic misfires on ordinary short text bubbles, and a false
    # "[AUDIO MESSAGE]" placeholder is worse than defaulting to text — which the
    # model corrects when it reads the crop. Kind is puntable by design.
    if h >= _IMG_MIN_H and fill >= _IMG_FILL_MIN and edge_energy < _IMG_EDGE_MAX:
        return "image"
    return "text"


# --------------------------------------------------------------------------- #
# segmentation
# --------------------------------------------------------------------------- #
def segment_messages(frame: Frame, slice, bg: np.ndarray) -> list[Band]:
    """Split ``frame``'s content slice into message :class:`Band` objects.

    Within ``frame.rgb[slice.top:slice.bottom]`` a row is *background* when almost
    all of it matches ``bg``; contiguous runs of non-background rows (bounded by
    background gutters) become message bands.  Sender is inferred from the
    horizontal center-of-mass of the foreground bubble mass (left -> "other",
    right -> "self", centered/full-width/ambiguous -> "unknown").  ``kind`` is a
    cheap shape guess.  Never raises.
    """
    top, bottom = int(slice.top), int(slice.bottom)
    bottom = min(bottom, frame.rgb.shape[0])
    top = max(0, min(top, bottom))
    content = frame.rgb[top:bottom]
    height, width = content.shape[0], content.shape[1]
    if height == 0 or width == 0:
        return []

    bg = np.asarray(bg, dtype=np.float32).reshape(3)
    fg = _foreground_mask(content, bg)                 # (H, W) bool
    fg_row = fg.mean(axis=1) > _ROW_FG_FRAC            # non-background rows

    spans = _merge_and_filter(_runs(fg_row), _MERGE_GAP, _MIN_BAND_H)

    center = width / 2.0
    left_thresh = center * (1.0 - _SIDE_MARGIN)
    right_thresh = center * (1.0 + _SIDE_MARGIN)
    cols = np.arange(width, dtype=np.float64)

    bands: list[Band] = []
    for rs, re in spans:
        band_fg = fg[rs:re]
        col_mass = band_fg.sum(axis=0).astype(np.float64)
        total = float(col_mass.sum())
        if total <= 0.0:
            continue

        com = float((cols * col_mass).sum() / total)
        xs = np.nonzero(col_mass > 0)[0]
        x0, x1 = int(xs[0]), int(xs[-1]) + 1
        span_w = x1 - x0

        if span_w >= _FULLWIDTH_FRAC * width:
            sender = "unknown"
        elif com < left_thresh:
            sender = "other"
        elif com > right_thresh:
            sender = "self"
        else:
            sender = "unknown"

        kind = _guess_kind(content[rs:re], band_fg)

        b_top, b_bottom = top + rs, top + re
        bbox = (x0, b_top, x1, b_bottom)
        bands.append(Band(b_top, b_bottom, sender, kind, bbox))

    return bands


# --------------------------------------------------------------------------- #
# cross-frame dedup
# --------------------------------------------------------------------------- #
def _signature(crop: np.ndarray) -> np.ndarray | None:
    """Zero-mean, unit-norm downsampled luma signature of a crop, or None if flat."""
    if crop.shape[0] == 0 or crop.shape[1] == 0:
        return None
    luma = crop.astype(np.float32) @ _LUMA
    h, w = luma.shape
    gh, gw = min(_SIG_GRID, h), min(_SIG_GRID, w)
    # Block-average onto a fixed grid so mismatched crop sizes still compare.
    r_idx = (np.arange(h) * gh // h)
    c_idx = (np.arange(w) * gw // w)
    grid = np.zeros((gh, gw), dtype=np.float64)
    counts = np.zeros((gh, gw), dtype=np.float64)
    np.add.at(grid, (r_idx[:, None], c_idx[None, :]), luma)
    np.add.at(counts, (r_idx[:, None], c_idx[None, :]), 1.0)
    grid /= np.maximum(counts, 1.0)
    v = grid.reshape(-1)
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    if n < 1e-8:
        return None
    return v / n


def _correlate(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Normalized correlation of two equal-length signatures (0.0 if either is None)."""
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    return float(np.dot(a, b))


def _save_crop(frame: Frame, band: Band, crops_dir: str, seq: int) -> str:
    """Save the full-width crop spanning ``band``'s rows as a high-res PNG; return path."""
    crop = frame.rgb[band.top:band.bottom, :]
    path = os.path.join(crops_dir, f"msg_{seq:04d}_f{frame.index}.png")
    save_png(crop, path)
    return path


@dataclass
class _Emitted:
    """Bookkeeping for one emitted message, so a duplicate can upgrade it in place."""

    eidx: int          # index into the emitted list
    frame_idx: int
    band: Band
    ctop: int          # band top in prev/own content coords
    cbottom: int       # band bottom in own content coords
    crop_png: str


def _match_prev(
    frame: Frame, slc, band: Band, ct: int, cb: int,
    prev_frame: Frame, prev_slc, cp: int, overlap: int,
    prev_emitted: list["_Emitted"],
) -> "_Emitted | None":
    """Return the prev emitted item this cur band duplicates, or None.

    The band's rows inside cur's top overlap zone are mapped to the corresponding
    prev content rows; the shared pixels are correlated and, on a strong match with
    a same-sender prev band covering that region, the prev item is returned.
    """
    zone_a = max(ct, 0)
    zone_b = min(cb, overlap)
    if zone_b - zone_a < _MIN_BAND_H:
        return None

    # Map the shared cur rows into prev content, then into prev frame coords.
    pt = cp - overlap + ct
    pb = cp - overlap + cb
    cur_crop = frame.rgb[slc.top + zone_a: slc.top + zone_b, :]
    prev_top = prev_slc.top + (cp - overlap + zone_a)
    prev_bot = prev_slc.top + (cp - overlap + zone_b)
    prev_crop = prev_frame.rgb[prev_top:prev_bot, :]
    if _correlate(_signature(cur_crop), _signature(prev_crop)) < _DEDUP_CORR:
        return None

    for item in prev_emitted:
        if item.band.sender != band.sender:
            continue
        lo, hi = max(pt, item.ctop), min(pb, item.cbottom)
        inter = max(0, hi - lo)
        denom = max(1, min(pb - pt, item.cbottom - item.ctop))
        if inter / denom >= _DEDUP_VSPAN:
            return item
    return None


def dedup_bands(
    frames: list[Frame],
    order: list[int],
    slices: list,
    overlaps: dict,
    bands_by_frame: dict,
    crops_dir: str | None = None,
) -> list[dict]:
    """Walk ``frames`` in ``order`` and emit each message once, de-duplicating overlaps.

    ``order`` lists frame indices top-to-bottom.  ``slices[i]`` is the
    :class:`~core.chrome.ContentSlice` of frame ``i``.  ``overlaps`` maps
    ``(prev_idx, cur_idx)`` to an :class:`~core.offset.OverlapResult`; its
    ``overlap`` is the number of shared content rows where the bottom of ``prev``
    meets the top of ``cur`` (``prev_content[-o:] ≈ cur_content[:o]``).
    ``bands_by_frame[i]`` is the :func:`segment_messages` output for frame ``i``.

    A band that sits wholly inside ``cur``'s top overlap zone and matches (sender +
    height + high downsampled pixel correlation) a band already emitted from
    ``prev`` is dropped; the earlier (prev) instance is kept.  Each surviving item
    is ``{"frame": idx, "band": Band, "crop_png": path}`` with a saved crop.
    """
    if crops_dir is None:
        crops_dir = tempfile.mkdtemp(prefix="unscroll_crops_")
    os.makedirs(crops_dir, exist_ok=True)

    emitted: list[dict] = []
    # frame_idx -> list of _Emitted for the bands emitted (fresh) from that frame.
    emitted_by_frame: dict[int, list[_Emitted]] = {}
    prev_idx: int | None = None

    for cur_idx in order:
        frame = frames[cur_idx]
        slc = slices[cur_idx]
        bands = bands_by_frame.get(cur_idx, [])

        overlap = 0
        if prev_idx is not None:
            ores = overlaps.get((prev_idx, cur_idx))
            if ores is not None:
                overlap = int(getattr(ores, "overlap", 0) or 0)

        prev_emitted = emitted_by_frame.get(prev_idx, []) if prev_idx is not None else []
        prev_slice = slices[prev_idx] if prev_idx is not None else None
        cp = (prev_slice.bottom - prev_slice.top) if prev_slice is not None else 0

        kept: list[_Emitted] = []
        for band in bands:
            ct = band.top - slc.top
            cb = band.bottom - slc.top

            # A band whose top lies inside cur's top overlap zone is shared content:
            # the same message must also appear near the bottom of prev.
            match = None
            if overlap > 0 and prev_slice is not None and ct < overlap:
                match = _match_prev(
                    frame, slc, band, ct, cb, frames[prev_idx], prev_slice,
                    cp, overlap, prev_emitted,
                )

            if match is not None:
                # Duplicate: keep whichever instance is more complete (taller).
                cur_h = band.bottom - band.top
                prev_h = match.band.bottom - match.band.top
                if cur_h > prev_h:
                    match.band = band
                    match.frame_idx = cur_idx
                    save_png(frame.rgb[band.top:band.bottom, :], match.crop_png)
                    emitted[match.eidx] = {
                        "frame": cur_idx, "band": band, "crop_png": match.crop_png,
                    }
                continue

            path = _save_crop(frame, band, crops_dir, len(emitted))
            item = _Emitted(
                eidx=len(emitted), frame_idx=cur_idx, band=band,
                ctop=ct, cbottom=cb, crop_png=path,
            )
            emitted.append({"frame": cur_idx, "band": band, "crop_png": path})
            kept.append(item)

        emitted_by_frame[cur_idx] = kept
        prev_idx = cur_idx

    return emitted


if __name__ == "__main__":  # pragma: no cover - manual smoke demo
    from tests import make_fixtures

    img, meta = make_fixtures.make_chat_with_meta(n_messages=10, seed=1)
    from core.imageio import content_hash, to_gray

    frm = Frame(
        id=content_hash(img), path="", rgb=img, gray=to_gray(img),
        width=img.shape[1], height=img.shape[0], source="screenshot",
        index=0, ts=None,
    )

    class _S:
        top, bottom, method, confidence = 0, img.shape[0], "structural", "low"

    bg = np.array([255, 255, 255], dtype=np.uint8)
    print("bands:", len(segment_messages(frm, _S(), bg)), "truth:", len(meta))
