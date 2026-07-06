"""core/offset.py — the vertical-overlap primitive (highest scrutiny).

Given two content-cropped luma frames ``A`` and ``B``, decide how many rows they
share where the *bottom* of ``A`` meets the *top* of ``B``.

Overlap semantics (authoritative, from CONTRACT.md — obeyed exactly)
--------------------------------------------------------------------
``vertical_offset(A, B)`` returns ``overlap`` = an integer number of rows such
that the **last ``overlap`` rows of A equal the first ``overlap`` rows of B**::

    A[H_A - overlap : H_A]  ≈  B[0 : overlap]

``overlap`` is expressed in *original content-row* coordinates (the caller slices
the un-downsampled arrays with it).

Algorithm
---------
1. Reduce each frame to a compact ``row_signature`` — rows downsampled by ``ds``
   and each row collapsed to ``K`` horizontal block-means.  This averages away
   JPEG ringing and anti-aliasing noise while preserving vertical structure.
2. Collapse each signature to a 1-D vertical profile and use an FFT normalized
   cross-correlation (``coarse_lag_fft``) to *nominate* a candidate overlap.
3. Around that nomination, evaluate a small window of candidate overlaps with a
   robust per-row ZNCC (zero-mean each row over its ``K`` features, normalized
   dot).  ``score = median(per_row_corr)`` is robust to a few mismatched rows
   (e.g. a status bar injected at the top of ``B``); ``inlier`` is the fraction of
   rows that agree strongly.  The peak-to-sidelobe ratio (``psr``) is the ratio of
   the winning score to the best non-adjacent score in the window.
4. Accept the best-scoring overlap iff it clears the ZNCC / PSR / inlier /
   min-overlap gates.

CONTRACT deviation (flagged): ``OverlapResult.per_row_corr`` is one value **per
signature row** in the overlap band — its length is ``overlap // ds``, not
``overlap``.  The module works entirely in signature space, so a per-signature-row
correlation vector is the natural (and non-fabricated) output.  ``overlap`` itself
is still returned in original content-row coordinates as the contract requires.

Pure stdlib + numpy.  No cv2 / scipy / torch.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# ---------------------------------------------------------------------------
# Module constants (validity gates) — from CONTRACT.md
# ---------------------------------------------------------------------------
ZNCC_MIN: float = 0.70        # median per-row ZNCC required for a valid overlap
PSR_MIN: float = 1.5          # peak-to-sidelobe ratio of the coarse correlation
INLIER_MIN: float = 0.5       # fraction of rows with per-row corr > INLIER_CORR

INLIER_CORR: float = 0.70     # per-row correlation counted as an "inlier"
_EPS: float = 1e-8

# Coarse search never considers an overlap below this many signature rows: a
# normalized correlation over a handful of samples is unstable (±1 by chance) and
# would corrupt both the peak search and the sidelobe (PSR) estimate. Excluding
# low-sample overlaps outright is cleaner than tapering, which would also weaken
# genuine short-overlap peaks.
_COARSE_MIN_SIG: int = 8
_PSR_GUARD: int = 3           # sig rows on each side of the peak excluded as sidelobe
_PSR_CAP: float = 12.0        # cap PSR when there is effectively no sidelobe
_DEFAULT_WINDOW: int = 16     # +/- signature rows scanned around the nomination


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class OverlapResult:
    """Outcome of a vertical-overlap test between two content frames."""

    overlap: int                          # shared rows (original coords); 0 if none
    score: float                          # median per-row ZNCC over the band, [-1,1]
    psr: float                            # peak-to-sidelobe ratio of coarse corr
    inlier: float                         # fraction of rows with corr > INLIER_CORR
    valid: bool                           # passed every validity gate
    confidence: str                       # "high" | "medium" | "low" | "none"
    per_row_corr: np.ndarray | None       # (overlap//ds,) float32, or None


# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------
def _as_luma(img: np.ndarray) -> np.ndarray:
    """Return a float32 luma ``(H, W)`` view of ``img`` (RGB or already gray)."""
    if img.ndim == 3:
        # Rec.601 luma; matches core/imageio.to_gray.
        rgb = img.astype(np.float32)
        return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    return img.astype(np.float32, copy=False)


def row_signature(img: np.ndarray, ds: int = 2, K: int = 48) -> np.ndarray:
    """Block-mean row signature of shape ``(H // ds, K)``.

    Rows are downsampled by ``ds`` (block-averaged, which also suppresses vertical
    AA/JPEG noise) and each downsampled row is split into ``K`` horizontal blocks
    reduced to their mean luma.  RGB input is converted to luma first.
    """
    if ds < 1:
        raise ValueError("ds must be >= 1")
    if K < 1:
        raise ValueError("K must be >= 1")

    luma = _as_luma(img)
    H, W = luma.shape
    Hd = H // ds
    if Hd < 1:
        # Degenerate: fewer than ``ds`` rows. Fall back to a single averaged row.
        Hd = 1
        rows = luma.mean(axis=0, keepdims=True)
    else:
        rows = luma[: Hd * ds].reshape(Hd, ds, W).mean(axis=1)

    # Horizontal block means. array_split tolerates W not divisible by K
    # (block widths differ by at most one column). K is clamped to W so no block
    # is empty for pathologically narrow inputs.
    k_eff = min(K, W)
    blocks = np.array_split(rows, k_eff, axis=1)
    sig = np.stack([b.mean(axis=1) for b in blocks], axis=1)

    if k_eff < K:  # pad to the contracted width by repeating the last column
        pad = np.repeat(sig[:, -1:], K - k_eff, axis=1)
        sig = np.concatenate([sig, pad], axis=1)

    return sig.astype(np.float32, copy=False)


# ---------------------------------------------------------------------------
# Coarse FFT nomination
# ---------------------------------------------------------------------------
def _next_pow2(n: int) -> int:
    return 1 << (int(n - 1).bit_length()) if n > 1 else 1


def _coarse_corr(
    profileA: np.ndarray,
    profileB: np.ndarray,
    min_sig: int = _COARSE_MIN_SIG,
) -> int:
    """Nominate an overlap from the normalized cross-correlation of two profiles.

    Returns the overlap ``o`` (in signature rows, ``>= min_sig``) whose alignment
    of the last ``o`` of ``profileA`` with the first ``o`` of ``profileB`` gives the
    highest normalized correlation.  Low-sample overlaps below ``min_sig`` are
    excluded (their normalized correlation is unstable).
    """
    a = np.asarray(profileA, dtype=np.float64)
    b = np.asarray(profileB, dtype=np.float64)
    a = a - a.mean()
    b = b - b.mean()
    La, Lb = a.size, b.size
    omax = min(La, Lb)
    if omax < 1:
        return 0

    N = _next_pow2(La + Lb)
    fa = np.fft.rfft(a, N)
    fb = np.fft.rfft(b, N)
    cc = np.fft.irfft(fa * np.conj(fb), N)  # cc[k] = sum_n a[n]*b[n-k]

    o = np.arange(1, omax + 1)
    dots = cc[La - o]                        # overlap o -> lag k = La - o
    a_tail_energy = np.cumsum((a[::-1]) ** 2)[:omax]   # energy of last o samples
    b_head_energy = np.cumsum(b ** 2)[:omax]           # energy of first o samples
    denom = np.sqrt(a_tail_energy * b_head_energy) + _EPS
    ncc = (dots / denom).astype(np.float64)

    floor = min(max(1, min_sig), omax)
    ncc[: floor - 1] = -np.inf                # exclude degenerate tiny overlaps
    return int(np.argmax(ncc)) + 1


def coarse_lag_fft(sigA: np.ndarray, sigB: np.ndarray) -> int:
    """FFT cross-correlation peak lag of the column-collapsed profiles.

    Collapses each ``(H, K)`` signature to a 1-D vertical profile (mean over the
    ``K`` features), zero-means, and cross-correlates via FFT (zero-padded to a
    power of two).  Returns the candidate overlap, in *signature rows*, at the
    normalized-correlation peak.
    """
    profileA = np.asarray(sigA, dtype=np.float64).mean(axis=1)
    profileB = np.asarray(sigB, dtype=np.float64).mean(axis=1)
    return _coarse_corr(profileA, profileB)


# ---------------------------------------------------------------------------
# Per-row ZNCC
# ---------------------------------------------------------------------------
def _per_row_zncc(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Per-row zero-normalized cross-correlation across the K features.

    ``A`` and ``B`` are ``(o, K)`` signature bands.  Each row is zero-meaned over
    its ``K`` features and the normalized dot product is taken.  Flat rows (no
    horizontal variation, e.g. a solid status bar) yield ~0 rather than a spurious
    ±1, so they neither help nor break the median.
    """
    A0 = A - A.mean(axis=1, keepdims=True)
    B0 = B - B.mean(axis=1, keepdims=True)
    num = np.einsum("ij,ij->i", A0, B0)
    den = np.sqrt(np.einsum("ij,ij->i", A0, A0) * np.einsum("ij,ij->i", B0, B0))
    corr = num / (den + _EPS)
    return corr.astype(np.float32, copy=False)


# A near-perfect ZNCC match is an unambiguous overlap even when PSR is weak. This
# happens with *periodic* content (evenly-spaced chat bubbles create correlation
# sidelobes close to the peak), where PSR is a poor ambiguity signal but the pixel
# agreement is not in doubt. Such a match is accepted (and can earn confidence)
# without PSR support; PSR still gates weaker matches against false positives.
_STRONG_SCORE: float = 0.90


def _confidence(score: float, psr: float, inlier: float) -> str:
    """Map validity margins to a coarse confidence label.

    A very strong pixel agreement (high ``score`` + ``inlier``) earns confidence
    even when ``psr`` is low, so periodic content is not perpetually rated "low".
    """
    if score >= 0.85 and inlier >= 0.75 and (psr >= 3.0 or score >= 0.97):
        return "high"
    if score >= 0.75 and inlier >= 0.6 and (psr >= 2.0 or score >= 0.90):
        return "medium"
    return "low"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def vertical_offset(
    grayA: np.ndarray,
    grayB: np.ndarray,
    min_overlap: int = 8,
    max_overlap: int | None = None,
    *,
    ds: int = 2,
    K: int = 48,
    window: int | None = None,
    zncc_min: float = ZNCC_MIN,
    psr_min: float = PSR_MIN,
    inlier_min: float = INLIER_MIN,
) -> OverlapResult:
    """Detect how many bottom rows of ``A`` overlap the top rows of ``B``.

    Returns an :class:`OverlapResult`; ``overlap`` is in original content rows such
    that ``A[H_A-overlap:H_A] ≈ B[0:overlap]``.  ``overlap`` is 0 and
    ``confidence`` is ``"none"`` when nothing clears the validity gates.

    The keyword-only knobs (``ds``, ``K``, ``window`` and the three thresholds) are
    driven by :func:`relaxed_vertical_offset`'s retry ladder; positional defaults
    match the contract exactly.
    """
    sigA = row_signature(grayA, ds, K)
    sigB = row_signature(grayB, ds, K)
    Ha, Hb = sigA.shape[0], sigB.shape[0]

    none_result = OverlapResult(0, 0.0, 0.0, 0.0, False, "none", None)

    omax_sig = min(Ha, Hb)
    if max_overlap is not None:
        omax_sig = min(omax_sig, max_overlap // ds)
    # Minimum overlap expressed in signature rows (round up so we never accept an
    # original-row overlap below ``min_overlap``).
    min_sig = max(1, -(-min_overlap // ds))
    if omax_sig < min_sig:
        return none_result

    # 1) Coarse FFT nomination (collapsed 1-D profile, per contract).
    cand_sig = coarse_lag_fft(sigA, sigB)

    # 2) Evaluate a window of candidate overlaps around the nomination with the
    #    per-row ZNCC and build the score curve S[o] = median per-row correlation.
    win = _DEFAULT_WINDOW if window is None else window
    lo = max(min_sig, cand_sig - win)
    hi = min(omax_sig, cand_sig + win)
    if hi < lo:
        return none_result

    offsets = list(range(lo, hi + 1))
    scores = np.empty(len(offsets), dtype=np.float64)
    best_o = 0
    best_score = -np.inf
    best_inlier = 0.0
    best_local = 0
    best_corr: np.ndarray | None = None
    for i, o in enumerate(offsets):
        corr = _per_row_zncc(sigA[Ha - o : Ha], sigB[0:o])
        score = float(np.median(corr))
        scores[i] = score
        if score > best_score:
            best_score = score
            best_o = o
            best_local = i
            best_inlier = float(np.mean(corr > INLIER_CORR))
            best_corr = corr

    if best_corr is None:
        return none_result

    # 3) PSR = peak-to-sidelobe of the ZNCC score curve. The 1-D coarse profile is
    #    too weak a signal for a reliable ratio (a few-sample normalized
    #    correlation is ±1 by chance); the per-row-ZNCC curve — each point backed
    #    by o*K samples — has a sharp, dominant peak whose ratio to the best
    #    non-adjacent competitor is a meaningful ambiguity measure.
    side = scores.copy()
    g_lo = max(0, best_local - _PSR_GUARD)
    g_hi = min(len(scores), best_local + _PSR_GUARD + 1)
    side[g_lo:g_hi] = -np.inf
    finite = side[np.isfinite(side)]
    peak = max(best_score, 0.0)
    if finite.size == 0 or peak <= _EPS:
        psr = _PSR_CAP if peak > _EPS else 0.0
    else:
        sidelobe = max(float(finite.max()), _EPS)
        psr = min(_PSR_CAP, peak / sidelobe)

    overlap = best_o * ds
    score = best_score
    inlier = best_inlier

    # PSR is a hard gate for ordinary matches, but a near-perfect ZNCC match (see
    # _STRONG_SCORE) is accepted without it — periodic chat layouts depress PSR
    # while the pixel agreement is unambiguous.
    strong = score >= _STRONG_SCORE and inlier >= inlier_min
    valid = (
        score >= zncc_min
        and inlier >= inlier_min
        and overlap >= min_overlap
        and (psr >= psr_min or strong)
    )
    if not valid:
        # Return diagnostics but no accepted overlap.
        return OverlapResult(0, score, psr, inlier, False, "none", None)

    confidence = _confidence(score, psr, inlier)
    return OverlapResult(overlap, score, psr, inlier, True, confidence, best_corr)


# ---------------------------------------------------------------------------
# Retry ladder
# ---------------------------------------------------------------------------
# Each rung progressively trades precision/speed for recall: less downsampling
# (finer rows), a wider candidate window, and relaxed acceptance thresholds. The
# order retry loop climbs the ladder before declaring a fatal overlap gap.
_LADDER: tuple[dict, ...] = (
    # level 0 — defaults: ds=2, default window, contract thresholds.
    dict(ds=2, window=16, zncc_min=0.70, psr_min=1.5, inlier_min=0.50),
    # level 1 — same resolution, wider search, slightly relaxed gates.
    dict(ds=2, window=16, zncc_min=0.65, psr_min=1.4, inlier_min=0.45),
    # level 2 — full resolution (ds=1), wider window, more relaxed gates.
    dict(ds=1, window=24, zncc_min=0.60, psr_min=1.3, inlier_min=0.40),
    # level 3 — full resolution, effectively exhaustive window, loosest gates.
    dict(ds=1, window=10_000, zncc_min=0.55, psr_min=1.2, inlier_min=0.35),
)


def relaxed_vertical_offset(
    grayA: np.ndarray,
    grayB: np.ndarray,
    level: int,
) -> OverlapResult:
    """Run :func:`vertical_offset` at retry-ladder rung ``level``.

    ``level`` is clamped to ``[0, len(_LADDER)-1]``.  Rung 0 reproduces the
    defaults; higher rungs reduce downsampling, widen the candidate window, and
    relax the ZNCC/PSR/inlier gates (see ``_LADDER``).
    """
    rung = _LADDER[max(0, min(level, len(_LADDER) - 1))]
    return vertical_offset(grayA, grayB, **rung)


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    rng = np.random.default_rng(0)
    parent = np.cumsum(rng.standard_normal((600, 320)).astype(np.float32), axis=0)
    parent = (parent - parent.min()) / (parent.max() - parent.min()) * 255.0
    A = parent[40:340]
    B = parent[300:560]  # true overlap = 40 rows
    print(vertical_offset(A, B))
