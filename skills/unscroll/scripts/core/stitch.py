"""core/stitch.py — assemble the validation image from the ordered frames.

The stitched PNG is a *validation artifact*: it lets a human confirm the capture
is complete and gapless. It is built by cropping each frame to its content slice
and concatenating the non-overlapping portions, cutting each join at the splice
row chosen inside that pair's overlap band.

Splice math (CONTRACT.md, obeyed exactly)
-----------------------------------------
For an adjacent pair ``a`` (upper) and ``b`` (lower) with overlap ``o`` and splice
``s in [0, o)`` — both measured in *content-row* coordinates within each frame's
content slice::

    a contributes content rows [0 : H_a_content - o + s]
    b contributes content rows [s : H_b_content]

so the join neither duplicates nor drops a row. Generalising across the whole
chain, each frame ``k`` (in ``order``) contributes content rows
``[start_k : end_k)`` where::

    start_k = 0                          if k is first
            = s(prev, k)                 otherwise   (splice with its predecessor)
    end_k   = H_k_content                if k is last
            = H_k_content - o(k, next) + s(k, next)  otherwise

``splices`` shape expected by :func:`stitch`
--------------------------------------------
``splices`` maps each adjacency ``(order[k], order[k+1])`` -> a dict::

    {"splice": SpliceResult, "overlap": int}

i.e. the chosen :class:`core.splice.SpliceResult` alongside the overlap it was
chosen within. :func:`compute_splices` produces exactly this dict (and is the ONE
place per-row-correlation resampling happens). ``stitch`` reads ``["splice"].s``
and ``["overlap"]`` for every join.

Pure stdlib + numpy. No cv2 / scipy / torch.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from core.splice import SpliceResult, select_splice_row

__all__ = ["stitch", "compute_splices", "segment_ranges", "segment_heights"]

# 1px neutral hairline drawn at each seam when ``mark`` (semi-transparent blend so
# it reads as a marker without fully destroying the underlying content row).
_MARK_RGB = np.array([128, 128, 128], dtype=np.float32)
_MARK_ALPHA = 0.6


def _content_rgb(frame: Any, slc: Any) -> np.ndarray:
    """Return ``frame``'s RGB cropped to its content slice ``[top:bottom]``."""
    rgb = np.asarray(frame.rgb)
    top = max(0, int(slc.top))
    bottom = min(rgb.shape[0], int(slc.bottom))
    if bottom <= top:
        return rgb
    return rgb[top:bottom]


def _resample_corr(per_row_corr: np.ndarray | None, overlap: int) -> np.ndarray | None:
    """Resample a signature-space per-row correlation to ``overlap`` rows.

    ``OverlapResult.per_row_corr`` has length ``overlap // ds`` (signature space),
    NOT ``overlap`` — resample it to content-row length before handing it to
    :func:`core.splice.select_splice_row`. Returns ``None`` when there is nothing
    to resample.
    """
    if per_row_corr is None or overlap <= 0:
        return None
    prc = np.asarray(per_row_corr, dtype=np.float32)
    if prc.size == 0:
        return None
    if prc.size == overlap:
        return prc
    xp = np.linspace(0.0, overlap - 1, num=prc.size)
    return np.interp(np.arange(overlap), xp, prc).astype(np.float32)


def compute_splices(
    frames: list[Any],
    order: list[int],
    slices: list[Any],
    matrix: dict[tuple[int, int], Any],
) -> dict[tuple[int, int], dict]:
    """Choose a splice row for every adjacency in ``order``.

    For each adjacency ``(a, b)`` pulls the overlap from ``matrix[(a, b)]``,
    resamples its ``per_row_corr`` to content-row length (the single place this
    resampling lives), and calls :func:`core.splice.select_splice_row` on frame
    ``b``'s content. Returns the ``{(a, b): {"splice": SpliceResult, "overlap": int}}``
    dict that :func:`stitch` consumes.
    """
    splices: dict[tuple[int, int], dict] = {}
    for a, b in zip(order, order[1:]):
        res = matrix.get((a, b))
        overlap = int(res.overlap) if res is not None else 0
        content_b = _content_rgb(frames[b], slices[b])
        corr = _resample_corr(getattr(res, "per_row_corr", None), overlap)
        sp = select_splice_row(content_b, overlap, corr)
        splices[(a, b)] = {"splice": sp, "overlap": overlap}
    return splices


def segment_ranges(
    frames: list[Any],
    order: list[int],
    slices: list[Any],
    splices: dict[tuple[int, int], dict],
) -> list[tuple[int, int, int]]:
    """Return each frame's ``(idx, start, end)`` content-row contribution to the stitch.

    This is the single source of truth for the splice math (module docstring):
    :func:`stitch` uses it to cut segments, and callers use it to verify the
    stitched height (``sum(end - start) == stitched_height``) as a corruption check.
    """
    ranges: list[tuple[int, int, int]] = []
    for pos, idx in enumerate(order):
        h = _content_rgb(frames[idx], slices[idx]).shape[0]

        # start = splice with predecessor (0 for the first frame).
        if pos == 0:
            start = 0
        else:
            prev = order[pos - 1]
            start = int(splices[(prev, idx)]["splice"].s)

        # end = H - o + s of the join with the successor (H for the last frame).
        if pos == len(order) - 1:
            end = h
        else:
            nxt = order[pos + 1]
            join = splices[(idx, nxt)]
            end = h - int(join["overlap"]) + int(join["splice"].s)

        start = int(np.clip(start, 0, h))
        end = int(np.clip(end, start, h))
        ranges.append((idx, start, end))
    return ranges


def segment_heights(
    frames: list[Any],
    order: list[int],
    slices: list[Any],
    splices: dict[tuple[int, int], dict],
) -> list[int]:
    """Per-frame contributed heights (``end - start``); their sum is the stitch height."""
    return [end - start for _idx, start, end in
            segment_ranges(frames, order, slices, splices)]


def stitch(
    frames: list[Any],
    order: list[int],
    slices: list[Any],
    splices: dict[tuple[int, int], dict],
    mark: bool = True,
) -> np.ndarray:
    """Assemble the ordered content segments into one tall RGB validation image.

    Concatenates each frame's content contribution per the contract splice math
    (see module docstring). When ``mark`` is set, a 1px neutral hairline is blended
    over each seam row. ``splices`` must be the
    ``{(a, b): {"splice": SpliceResult, "overlap": int}}`` mapping from
    :func:`compute_splices`.
    """
    if not order:
        return np.zeros((0, 0, 3), dtype=np.uint8)

    contents = [_content_rgb(frames[idx], slices[idx]) for idx in order]
    ranges = segment_ranges(frames, order, slices, splices)
    segments = [contents[pos][start:end]
                for pos, (_idx, start, end) in enumerate(ranges)]

    stitched = np.ascontiguousarray(np.concatenate(segments, axis=0)).astype(np.uint8)

    if mark:
        # Seam rows sit at the cumulative boundary between consecutive segments.
        row = 0
        for seg in segments[:-1]:
            row += seg.shape[0]
            if 0 <= row < stitched.shape[0]:
                blended = (
                    (1.0 - _MARK_ALPHA) * stitched[row].astype(np.float32)
                    + _MARK_ALPHA * _MARK_RGB
                )
                stitched[row] = np.clip(blended, 0, 255).astype(np.uint8)

    return stitched
