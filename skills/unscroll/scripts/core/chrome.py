"""Registry-free chrome (status bar / nav / input bar / keyboard) detection.

Chrome boundaries are detected at runtime instead of being hardcoded per device.

Two methods are provided:

* ``temporal_chrome_mask`` — strong signal.  Given a stack of same-resolution luma
  frames of the same app, chrome sits at the same absolute rows and is near-identical
  across frames while message content scrolls.  Per-row temporal variance is low over
  chrome and high over content.
* ``structural_content_slice`` — single-image fallback.  Chrome bands are near
  constant-color horizontal runs (low row variance and/or a high fraction of
  background-colored pixels).

Coordinates: rows increase downward.  A :class:`ContentSlice` is ``(top, bottom)`` with
``bottom`` exclusive, bounding the message-content region after chrome is masked off.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = [
    "ContentSlice",
    "temporal_chrome_mask",
    "structural_content_slice",
    "content_slices",
]


@dataclass
class ContentSlice:
    """Row bounds of the message-content region of a frame (``bottom`` exclusive)."""

    top: int
    bottom: int
    method: str        # "temporal" | "structural"
    confidence: str    # "high" | "medium" | "low"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _leading_low_run(vec: np.ndarray, thresh: float) -> int:
    """Return length of the leading contiguous run of ``vec < thresh``."""
    run = 0
    for value in vec:
        if value < thresh:
            run += 1
        else:
            break
    return run


def _trailing_low_run(vec: np.ndarray, thresh: float) -> int:
    """Return length of the trailing contiguous run of ``vec < thresh``."""
    run = 0
    for value in vec[::-1]:
        if value < thresh:
            run += 1
        else:
            break
    return run


def _modal_value_and_frac(gray: np.ndarray) -> tuple[float, np.ndarray]:
    """Return the modal luma value and the per-row fraction of near-modal pixels."""
    quant = np.clip(np.rint(gray), 0, 255).astype(np.int32)
    counts = np.bincount(quant.reshape(-1), minlength=256)
    modal = float(np.argmax(counts))
    tol = 12.0
    near = np.abs(gray - modal) <= tol
    bg_frac = near.mean(axis=1).astype(np.float32)
    return modal, bg_frac


# --------------------------------------------------------------------------- #
# temporal method
# --------------------------------------------------------------------------- #
def temporal_chrome_mask(grays: list[np.ndarray]) -> ContentSlice:
    """Detect chrome from a stack of same-resolution luma frames via temporal variance."""
    stack = np.stack([np.asarray(g, dtype=np.float32) for g in grays], axis=0)
    n, height, _ = stack.shape

    # Per-(row,col) variance over frames, then mean across columns -> length-H vector.
    row_var = stack.var(axis=0).mean(axis=1).astype(np.float32)

    if n < 2 or height == 0:
        # Not enough frames to separate chrome from content temporally.
        return ContentSlice(0, height, "temporal", "low")

    peak = float(row_var.max())
    floor = float(row_var.min())
    if peak <= 1e-6:
        # Every row identical across frames -> no scrolling content detected.
        return ContentSlice(0, height, "temporal", "low")

    # Adaptive threshold sitting just above the low (chrome) plateau.
    thresh = floor + 0.10 * (peak - floor)

    top = _leading_low_run(row_var, thresh)
    bottom = height - _trailing_low_run(row_var, thresh)

    if bottom <= top:
        # Degenerate separation; hand back the whole frame at low confidence.
        return ContentSlice(0, height, "temporal", "low")

    # Confidence: crisp when the chrome plateau is essentially flat relative to content.
    content_median = float(np.median(row_var[top:bottom]))
    chrome_rows = np.concatenate([row_var[:top], row_var[bottom:]])
    chrome_level = float(chrome_rows.max()) if chrome_rows.size else 0.0
    if content_median <= 1e-6:
        confidence = "low"
    elif chrome_level <= 0.05 * content_median and (top > 0 or bottom < height):
        confidence = "high"
    elif chrome_level <= 0.25 * content_median:
        confidence = "medium"
    else:
        confidence = "low"

    return ContentSlice(top, bottom, "temporal", confidence)


# --------------------------------------------------------------------------- #
# structural method
# --------------------------------------------------------------------------- #
def structural_content_slice(gray: np.ndarray) -> ContentSlice:
    """Detect chrome from a single luma image via row variance + background fraction."""
    gray = np.asarray(gray, dtype=np.float32)
    height, _ = gray.shape
    if height == 0:
        return ContentSlice(0, 0, "structural", "low")

    row_var = gray.var(axis=1).astype(np.float32)
    _, bg_frac = _modal_value_and_frac(gray)

    peak = float(row_var.max())
    # Rows are "flat" (near constant color) when their variance is tiny relative to the
    # busiest row.  A row is chrome-like when it is flat OR predominantly background.
    var_thresh = max(5.0, 0.02 * peak)
    flat = row_var < var_thresh
    bg_high = bg_frac > 0.90
    chromeish = flat | bg_high

    # Walk inward from both ends while rows look like chrome.
    top = 0
    while top < height and chromeish[top]:
        top += 1
    bottom = height
    while bottom > top and chromeish[bottom - 1]:
        bottom -= 1

    if bottom <= top:
        # Whole image looked like chrome (or empty); punt with the full frame.
        return ContentSlice(0, height, "structural", "low")

    # Confidence is inherently lower for the single-image heuristic.
    stripped = top + (height - bottom)
    confidence = "medium" if stripped > 0 else "low"
    return ContentSlice(top, bottom, "structural", confidence)


# --------------------------------------------------------------------------- #
# orchestrator
# --------------------------------------------------------------------------- #
def content_slices(frames: list) -> list[ContentSlice]:
    """Return one :class:`ContentSlice` per frame, in original order.

    Frames are grouped by ``(height, width)``.  Groups with >= 3 frames use the temporal
    method (all frames in the group share the detected slice); smaller groups fall back to
    the structural method per frame.  ``frames`` items must expose ``.gray``, ``.height``
    and ``.width`` (e.g. :class:`core.imageio.Frame`).
    """
    groups: dict[tuple[int, int], list[int]] = {}
    for idx, frame in enumerate(frames):
        groups.setdefault((frame.height, frame.width), []).append(idx)

    result: list[ContentSlice | None] = [None] * len(frames)
    for indices in groups.values():
        if len(indices) >= 3:
            grays = [np.asarray(frames[i].gray, dtype=np.float32) for i in indices]
            shared = temporal_chrome_mask(grays)
            for i in indices:
                result[i] = ContentSlice(
                    shared.top, shared.bottom, shared.method, shared.confidence
                )
        else:
            for i in indices:
                result[i] = structural_content_slice(
                    np.asarray(frames[i].gray, dtype=np.float32)
                )

    return [slc for slc in result if slc is not None]
