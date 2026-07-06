"""Splice-row selection inside a validated overlap band.

Given the overlap between two adjacent frames, choose a cut row ``s`` in ``[0, overlap)``
that lands in a *gutter* — a horizontal gap between message bubbles — so the stitched join
neither duplicates nor drops content.  A good gutter has low horizontal luma-gradient
energy (the ``edge`` signal, robust to patterned wallpaper) and a high fraction of
background-colored pixels (``bg_frac``).

Coordinate semantics (see CONTRACT.md): a cut ``s`` means frame A contributes rows
``[0 : H_A - overlap + s]`` and frame B contributes rows ``[s : H_B]``; the gutter is
searched within the first ``overlap`` rows of B's content.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["SpliceResult", "gutter_profile", "select_splice_row"]

_LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)
_BG_TOL = 16.0          # per-channel tolerance for "matches background"
_BG_HI = 0.85           # bg_frac above this counts as gutter-like
_MIN_BAND = 3           # preferred minimum gutter band width (px)


@dataclass
class SpliceResult:
    """Chosen splice cut ``s`` in ``[0, overlap)`` and the gutter band it sits in."""

    s: int
    confidence: str    # "high" | "medium" | "low"
    band_width: int    # width of the gutter band the cut sits in (px)


def _modal_color(rgb: np.ndarray) -> np.ndarray:
    """Return the modal (background) RGB color via an 8-bit-binned histogram mode.

    Computed inline so this module stays importable without ``core.imageio``.
    """
    flat = rgb.reshape(-1, 3).astype(np.int64)
    keys = (flat[:, 0] << 16) | (flat[:, 1] << 8) | flat[:, 2]
    values, counts = np.unique(keys, return_counts=True)
    key = int(values[int(np.argmax(counts))])
    return np.array([(key >> 16) & 255, (key >> 8) & 255, key & 255], dtype=np.uint8)


def gutter_profile(
    content_rgb: np.ndarray, bg: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Return per-row ``(bg_frac, edge)`` arrays.

    ``bg_frac[r]`` is the fraction of row ``r`` whose pixels are within tolerance of ``bg``.
    ``edge[r]`` is the mean absolute horizontal luma gradient of row ``r`` (low in gutters,
    high across textured bubbles); it is the more robust of the two signals.
    """
    rgb = np.asarray(content_rgb, dtype=np.float32)
    bg = np.asarray(bg, dtype=np.float32).reshape(3)

    within = np.all(np.abs(rgb - bg) <= _BG_TOL, axis=2)
    bg_frac = within.mean(axis=1).astype(np.float32)

    luma = rgb @ _LUMA
    if luma.shape[1] < 2:
        edge = np.zeros(luma.shape[0], dtype=np.float32)
    else:
        edge = np.abs(np.diff(luma, axis=1)).mean(axis=1).astype(np.float32)
    return bg_frac, edge


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


def select_splice_row(
    content_rgb: np.ndarray,
    overlap: int,
    per_row_corr: np.ndarray | None = None,
) -> SpliceResult:
    """Choose a gutter cut ``s`` in ``[0, overlap)``; never fails.

    Scores candidate gutter bands by width (>= 3px preferred), proximity to the overlap
    midpoint, and — when ``per_row_corr`` is supplied — per-row correlation.  If no clean
    gutter exists, returns the global-minimum-edge row with ``low`` confidence.
    """
    height = content_rgb.shape[0]
    band = min(int(overlap), height)
    if band <= 0:
        return SpliceResult(0, "low", 1)

    bg = _modal_color(content_rgb)
    bg_frac, edge = gutter_profile(content_rgb, bg)
    bg_frac = bg_frac[:band]
    edge = edge[:band]

    e_min = float(edge.min())
    e_max = float(edge.max())
    e_span = e_max - e_min
    eps = 1e-6

    # Rows that look like a gutter: low edge energy and/or predominantly background.
    edge_thresh = e_min + 0.20 * e_span
    gutter = (edge <= edge_thresh) & (bg_frac >= _BG_HI)
    if not gutter.any():
        gutter = edge <= edge_thresh  # fall back to the edge signal alone

    mid = (band - 1) / 2.0
    corr = None if per_row_corr is None else np.asarray(per_row_corr, dtype=np.float32)

    best = None
    best_score = -np.inf
    for start, end in _runs(gutter):
        width = end - start
        center = (start + end - 1) // 2
        proximity = 1.0 - abs(center - mid) / (mid + eps)          # [0, 1]
        edge_quality = 1.0 - (float(edge[start:end].mean()) - e_min) / (e_span + eps)
        corr_score = float(corr[start:end].mean()) if corr is not None else 0.0
        width_factor = min(width, 5) / 5.0
        score = (
            2.0 * (width >= _MIN_BAND)
            + width_factor
            + proximity
            + edge_quality
            + corr_score
        )
        if score > best_score:
            best_score = score
            best = (start, end, width, center)

    if best is None:
        # No gutter rows at all -> safest single row is the global edge minimum.
        s = int(np.argmin(edge))
        return SpliceResult(s, "low", 1)

    start, end, width, center = best
    s = int(min(max(center, 0), band - 1))

    band_edge = float(edge[start:end].mean())
    clean = band_edge <= e_min + 0.10 * e_span
    corr_ok = corr is None or float(corr[start:end].mean()) >= 0.7
    if width >= _MIN_BAND and clean and corr_ok:
        confidence = "high"
    elif width >= 1 and clean:
        confidence = "medium"
    else:
        confidence = "low"

    return SpliceResult(s, confidence, int(width))
