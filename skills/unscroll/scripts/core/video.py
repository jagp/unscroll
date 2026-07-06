"""core/video.py — VIDEO intake for unscroll.

Turns a scroll-capture screen recording into the minimal set of sharp,
mutually-overlapping still frames (the equivalent of good screenshots), which
then feed the existing screenshot pipeline.

Design: **dumb extraction, smart selection.**

1. ``extract_candidates`` shells out to ``ffmpeg`` (pass-1) to dump a dense,
   downscaled JPEG frame stream — cheap and dumb.
2. ``scroll_sharpness`` / ``inter_frame_shift`` / ``select_keyframes`` do all the
   thinking in numpy: reject motion-blurred frames, dedup paused frames, reject
   inertial end-of-scroll bounce, and greedily pick the farthest-apart frames
   that still overlap enough for :func:`core.offset.vertical_offset` to stitch.
3. ``video_to_frames`` re-extracts only the chosen frames at FULL resolution
   (pass-2, per-timestamp ffmpeg seek) and returns :class:`core.imageio.Frame`
   objects ready for the screenshot pipeline.

Standard library + numpy + Pillow only; ffmpeg is invoked as an external binary
via ``subprocess`` (never a python binding). Every subprocess call passes an
explicit timeout and its return code is checked.
"""

from __future__ import annotations

import glob
import os
import subprocess
from dataclasses import dataclass

import numpy as np

from core.imageio import load_image, to_gray, make_frame, Frame
from core.offset import coarse_lag_fft, row_signature, vertical_offset

__all__ = [
    "extract_candidates",
    "scroll_sharpness",
    "inter_frame_shift",
    "select_keyframes",
    "video_to_frames",
    "SelectionResult",
]

# --- tunables ---------------------------------------------------------------
_EXTRACT_TIMEOUT = 300      # seconds for the pass-1 dense extraction
_SEEK_TIMEOUT = 60          # seconds for a single pass-2 seek+decode
_DS = 2                     # row_signature downsample used for shift mapping
# A frame whose directional sharpness is below this fraction of the running
# median sharpness is treated as motion-blurred and rejected.
_BLUR_FRAC = 0.45
# Two frames are "the same paused view" when their shift is within this many
# pixels of zero AND their normalized mean-abs-difference is below _PAUSE_MAD.
_PAUSE_SHIFT_PX = 2
_PAUSE_MAD = 3.0            # mean |A-B| luma threshold for near-identical frames
# A trailing frame is a candidate inertial-bounce artifact when the scroll sign
# reverses relative to the dominant direction and a large uniform (low-variance)
# band appears — see ``_reject_inertial_bounce``.
_BLANK_ROW_VAR = 4.0       # per-row luma variance below this ~= uniform band
_BLANK_BAND_FRAC = 0.30    # fraction of rows uniform to call a frame "blanked"


@dataclass
class SelectionResult:
    """Side-channel diagnostics from :func:`select_keyframes`.

    ``keep`` is the authoritative list of kept candidate indices; the rest are
    non-fatal notes (never raised) so the caller can surface coverage warnings.
    """

    keep: list[int]
    dropped_blur: list[int]
    dropped_pause: list[int]
    dropped_bounce: list[int]
    coverage_gaps: list[int]   # kept indices reached via an over-large jump


# ---------------------------------------------------------------------------
# Pass 1 — dumb extraction
# ---------------------------------------------------------------------------
def extract_candidates(
    video_path: str,
    workdir: str,
    fps: int = 15,
    scale: float = 0.5,
) -> list[str]:
    """ffmpeg pass-1: dump a dense downscaled JPEG frame stream.

    Samples ``video_path`` at ``fps`` frames/second, scaling each frame by the
    fraction ``scale`` (``iw*scale:ih*scale``), and writes them as
    ``<workdir>/cand_%06d.jpg``. Returns the sorted list of frame paths.
    """
    os.makedirs(workdir, exist_ok=True)
    vf = f"fps={fps},scale=iw*{scale}:ih*{scale}"
    pattern = os.path.join(workdir, "cand_%06d.jpg")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vf", vf, "-q:v", "3", pattern],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_EXTRACT_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "ffmpeg pass-1 extraction failed: "
            + proc.stderr.decode("utf-8", "replace")[-800:]
        )
    return sorted(glob.glob(os.path.join(workdir, "cand_*.jpg")))


# ---------------------------------------------------------------------------
# Frame metrics
# ---------------------------------------------------------------------------
def scroll_sharpness(gray: np.ndarray) -> float:
    """Directional sharpness = variance of the vertical 2nd derivative.

    ``var(gray[2:] - 2*gray[1:-1] + gray[:-2])``. Higher = sharper; a low value
    means the frame is smeared vertically (scroll motion blur). Directional
    because scroll blur smears content along the scroll (vertical) axis, so the
    vertical 2nd derivative collapses on blurred frames while horizontal edges
    (unaffected) would not reveal it.
    """
    g = np.asarray(gray, dtype=np.float32)
    if g.shape[0] < 3:
        return 0.0
    lap_v = g[2:] - 2.0 * g[1:-1] + g[:-2]
    return float(np.var(lap_v))


def inter_frame_shift(grayA: np.ndarray, grayB: np.ndarray) -> int:
    """Signed vertical scroll displacement (pixels) from ``grayA`` to ``grayB``.

    Uses :func:`core.offset.coarse_lag_fft` on the row signatures in both
    directions and keeps whichever direction yields the larger overlap (the true
    motion aligns the bottom of one frame with the top of the other). Positive =
    downward scroll (content moved up; B is lower in the source than A);
    negative = upward scroll (an inertial bounce reverses the sign).
    """
    sigA = row_signature(grayA, ds=_DS)
    sigB = row_signature(grayB, ds=_DS)
    n_sig = min(sigA.shape[0], sigB.shape[0])

    # Forward: bottom of A meets top of B (downward scroll).
    o_fwd = coarse_lag_fft(sigA, sigB)
    # Reverse: bottom of B meets top of A (upward scroll / bounce).
    o_rev = coarse_lag_fft(sigB, sigA)

    if o_fwd >= o_rev:
        shift_sig = n_sig - o_fwd
        sign = 1
    else:
        shift_sig = n_sig - o_rev
        sign = -1
    return int(sign * shift_sig * _DS)


# ---------------------------------------------------------------------------
# Selection helpers
# ---------------------------------------------------------------------------
def _mean_abs_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute luma difference between two same-shape gray frames."""
    if a.shape != b.shape:
        h = min(a.shape[0], b.shape[0])
        w = min(a.shape[1], b.shape[1])
        a, b = a[:h, :w], b[:h, :w]
    return float(np.mean(np.abs(a.astype(np.float32) - b.astype(np.float32))))


def _uniform_band_fraction(gray: np.ndarray) -> float:
    """Fraction of rows whose horizontal variance is ~0 (a uniform blank band)."""
    g = np.asarray(gray, dtype=np.float32)
    row_var = g.var(axis=1)
    return float(np.mean(row_var < _BLANK_ROW_VAR))


def _running_median(values: list[float], i: int, half: int = 6) -> float:
    """Median of ``values`` in a window of radius ``half`` centered on ``i``."""
    lo = max(0, i - half)
    hi = min(len(values), i + half + 1)
    return float(np.median(values[lo:hi]))


def _reject_inertial_bounce(
    shifts: list[int],
    uniform: list[float],
    dominant_sign: int,
) -> list[int]:
    """Return trailing indices that look like end-of-scroll inertial bounce.

    An inertial bounce shows up as one or more TRAILING frames whose scroll sign
    reverses against the dominant direction and/or that expose a large uniform
    (blank) band scrolled past the content edge. Only a contiguous run at the very
    end is rejected — a mid-stream direction change is a legitimate user scroll.
    """
    n = len(shifts)
    bounce: list[int] = []
    for i in range(n - 1, 0, -1):
        reversed_dir = dominant_sign != 0 and np.sign(shifts[i]) == -dominant_sign
        blanked = uniform[i] >= _BLANK_BAND_FRAC
        if reversed_dir or blanked:
            bounce.append(i)
        else:
            break
    return sorted(bounce)


def select_keyframes(
    candidate_paths: list[str],
    target_overlap: float = 0.35,
    *,
    return_details: bool = False,
) -> list[int] | SelectionResult:
    """Pick indices of sharp, settled, mutually-overlapping candidate frames.

    Pipeline:
      (a) per-frame directional sharpness + signed inter-frame shift;
      (b) DEDUP paused frames (shift≈0 and near-identical), keeping the sharpest;
      (c) REJECT motion-blurred frames (sharpness below an adaptive fraction of
          the running median);
      (d) REJECT trailing inertial-bounce frames (reversed scroll sign / uniform
          blank band at the very end);
      (e) greedy OVERLAP-CONSTRAINED walk: from the first good frame, advance to
          the FARTHEST later good frame whose measured displacement from the last
          kept frame still leaves >= ``target_overlap`` of the frame height
          overlapping. If even the next frame overshoots, keep it anyway and flag
          a coverage gap (never crash).

    Returns the list of kept indices, or a :class:`SelectionResult` with
    diagnostics when ``return_details`` is set.
    """
    n = len(candidate_paths)
    if n == 0:
        empty = SelectionResult([], [], [], [], [])
        return empty if return_details else []
    if n == 1:
        one = SelectionResult([0], [], [], [], [])
        return one if return_details else [0]

    grays = [to_gray(load_image(p)) for p in candidate_paths]
    H = grays[0].shape[0]

    sharp = [scroll_sharpness(g) for g in grays]
    shifts = [0] + [inter_frame_shift(grays[i - 1], grays[i]) for i in range(1, n)]
    uniform = [_uniform_band_fraction(g) for g in grays]

    # --- (b) dedup paused runs: group near-identical consecutive frames -----
    dropped_pause: list[int] = []
    alive = [True] * n
    i = 0
    while i < n:
        j = i + 1
        while (
            j < n
            and abs(shifts[j]) <= _PAUSE_SHIFT_PX
            and _mean_abs_diff(grays[j - 1], grays[j]) <= _PAUSE_MAD
        ):
            j += 1
        if j - i > 1:  # a paused run [i, j) — keep only the sharpest member
            best = max(range(i, j), key=lambda k: sharp[k])
            for k in range(i, j):
                if k != best:
                    alive[k] = False
                    dropped_pause.append(k)
        i = j

    # --- (c) reject motion-blurred frames (adaptive threshold) --------------
    dropped_blur: list[int] = []
    for k in range(n):
        if not alive[k]:
            continue
        thresh = _BLUR_FRAC * _running_median(sharp, k)
        if sharp[k] < thresh:
            alive[k] = False
            dropped_blur.append(k)

    # --- (d) reject trailing inertial-bounce frames -------------------------
    signs = [int(np.sign(s)) for s in shifts[1:] if s != 0]
    dominant_sign = int(np.sign(sum(signs))) if signs else 0
    bounce = _reject_inertial_bounce(shifts, uniform, dominant_sign)
    dropped_bounce: list[int] = []
    for k in bounce:
        if alive[k]:
            alive[k] = False
            dropped_bounce.append(k)

    good = [k for k in range(n) if alive[k]]
    if not good:  # everything got rejected — fall back to the sharpest frame
        best = max(range(n), key=lambda k: sharp[k])
        res = SelectionResult([best], dropped_blur, dropped_pause, dropped_bounce, [])
        return res if return_details else res.keep

    # --- (e) greedy overlap-constrained selection ---------------------------
    # Absolute scroll position of every candidate, accumulated from the reliable
    # consecutive-frame shifts. (Measuring the shift between two DISTANT frames
    # directly is unreliable — once they no longer overlap, coarse_lag_fft has no
    # true peak — so we integrate the trustworthy adjacent shifts instead.)
    position = np.cumsum(np.asarray(shifts, dtype=np.float64))
    # Keep >= target_overlap of frame height overlapping between kept frames,
    # i.e. the displacement between kept frames must stay <= (1-overlap) * H.
    budget = (1.0 - target_overlap) * H
    keep = [good[0]]
    coverage_gaps: list[int] = []
    last = good[0]
    pos = 1
    while pos < len(good):
        # Advance as far as the budget allows, then commit that frame.
        chosen = None
        p = pos
        while p < len(good):
            disp = abs(position[good[p]] - position[last])
            if disp <= budget:
                chosen = p
                p += 1
            else:
                break
        if chosen is not None:
            keep.append(good[chosen])
            last = good[chosen]
            pos = chosen + 1
        else:
            # Even the very next good frame overshoots the budget: keep it so we
            # never drop coverage, but flag a potential gap. Never crash.
            nxt = good[pos]
            keep.append(nxt)
            coverage_gaps.append(nxt)
            last = nxt
            pos += 1

    res = SelectionResult(keep, dropped_blur, dropped_pause, dropped_bounce, coverage_gaps)
    return res if return_details else res.keep


# ---------------------------------------------------------------------------
# Pass 2 — full-resolution re-extraction of the chosen frames
# ---------------------------------------------------------------------------
def _seek_full_res(video_path: str, t: float, out_png: str) -> bool:
    """ffmpeg pass-2: decode a single full-res frame at timestamp ``t`` seconds."""
    proc = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{t:.6f}", "-i", video_path,
         "-frames:v", "1", "-q:v", "1", out_png],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=_SEEK_TIMEOUT,
    )
    return proc.returncode == 0 and os.path.isfile(out_png)


def video_to_frames(
    video_path: str,
    workdir: str,
    fps: int = 15,
    scale: float = 0.5,
) -> list[Frame]:
    """Full VIDEO intake: pass-1 candidates → select → pass-2 full-res frames.

    Extracts dense downscaled candidates, selects the sharp mutually-overlapping
    keyframes, then re-extracts each chosen frame at full resolution via a
    per-timestamp ffmpeg seek (``t = candidate_index / fps``). Returns
    :class:`core.imageio.Frame` objects with ``source="video"`` and sequential
    ``index``, ready to enter the screenshot pipeline.
    """
    os.makedirs(workdir, exist_ok=True)
    candidates = extract_candidates(video_path, workdir, fps=fps, scale=scale)
    if not candidates:
        return []

    keep = select_keyframes(candidates)

    full_dir = os.path.join(workdir, "keyframes")
    os.makedirs(full_dir, exist_ok=True)

    frames: list[Frame] = []
    seq = 0
    for cand_idx in keep:
        t = cand_idx / float(fps)
        out_png = os.path.join(full_dir, f"key_{seq:04d}.png")
        if not _seek_full_res(video_path, t, out_png):
            # Puntable: a single unseekable timestamp must not crash intake.
            continue
        frames.append(make_frame(out_png, source="video", index=seq, ts=t))
        seq += 1
    return frames


if __name__ == "__main__":  # pragma: no cover - manual smoke demo
    import sys

    if len(sys.argv) == 3:
        fr = video_to_frames(sys.argv[1], sys.argv[2])
        print(f"kept {len(fr)} full-res frames")
        for f in fr:
            print(f"  index={f.index} {f.width}x{f.height} ts={f.ts:.3f} id={f.id}")
