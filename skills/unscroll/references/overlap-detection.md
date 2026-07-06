# Overlap detection — the `vertical_offset` primitive

How unscroll decides that the bottom of one frame is the top of the next. This is the single
most important primitive: ordering, stitching, and dedup all rest on it. Implemented in
`core/offset.py`; consumed by `core/order.py`.

## What it computes

`vertical_offset(grayA, grayB)` returns an `OverlapResult` whose `overlap` is the number of rows
such that

```
A[H_A - overlap : H_A]  ≈  B[0 : overlap]
```

i.e. the last `overlap` rows of A equal the first `overlap` rows of B. `A` and `B` are luma
frames already cropped to their content slices (chrome removed). `overlap` is expressed in
original content-row coordinates; it is `0` (and `confidence` is `"none"`) when nothing clears
the validity gates.

`OverlapResult` fields: `overlap`, `score` (median per-row ZNCC over the band, in [-1, 1]),
`psr` (peak-to-sidelobe ratio), `inlier` (fraction of rows with per-row correlation above 0.70),
`valid` (bool), `confidence` (`high`/`medium`/`low`/`none`), and `per_row_corr`.

## Algorithm

1. **Row signature** (`row_signature`, default `ds=2`, `K=48`) — reduce each frame to an
   `(H//ds, K)` block-mean signature: rows downsampled by `ds` (block-averaged, which also
   suppresses vertical anti-aliasing / JPEG noise), each downsampled row collapsed to `K`
   horizontal block means. RGB is converted to Rec.601 luma first.

2. **Coarse FFT nomination** (`coarse_lag_fft`) — collapse each signature to a 1-D vertical
   profile (mean over the `K` features), zero-mean, and take the FFT normalized cross-correlation
   (zero-padded to a power of two). The normalized-correlation peak *nominates* a candidate
   overlap in signature rows. Overlaps below a small floor (`_COARSE_MIN_SIG = 8` signature rows)
   are excluded — a normalized correlation over a handful of samples is ±1 by chance and would
   corrupt both the peak and the sidelobe estimate.

3. **Per-row ZNCC refinement** — evaluate a window of candidate overlaps (`±16` signature rows by
   default) around the nomination. For each candidate, take the per-row zero-normalized
   cross-correlation across the `K` features; `score = median(per_row_corr)`. The median is
   robust to a few mismatched rows (e.g. a status bar injected at the top of B). Flat rows (a
   solid status bar) yield ~0 rather than a spurious ±1, so they neither help nor break the
   median. `inlier` is the fraction of rows correlating above `INLIER_CORR = 0.70`.

4. **PSR** — the peak-to-sidelobe ratio is taken from the *ZNCC score curve* over the window
   (each point backed by `overlap × K` samples), not the weak 1-D coarse profile: the winning
   score divided by the best non-adjacent competitor (a guard of ±3 rows around the peak is
   excluded as sidelobe). It is capped at `12.0` when there is effectively no sidelobe.

## Confidence gates

Module constants (from CONTRACT.md): `ZNCC_MIN = 0.70`, `PSR_MIN = 1.5`, `INLIER_MIN = 0.5`.
An overlap is **valid** iff:

```
score  >= ZNCC_MIN
inlier >= INLIER_MIN
overlap >= min_overlap            (default 8 original rows)
AND (psr >= PSR_MIN  OR  strong)
```

where `strong = score >= 0.90 and inlier >= INLIER_MIN`. The strong-match escape matters for
**periodic layouts**: evenly-spaced chat bubbles create correlation sidelobes close to the peak,
which depresses PSR even though the pixel agreement is unambiguous. A near-perfect ZNCC match is
therefore accepted without PSR support; PSR still gates *weaker* matches against false positives.

The `confidence` label is a coarser bucket of the same margins: `high` needs `score >= 0.85`,
`inlier >= 0.75`, and `psr >= 3.0` (or a near-perfect `score >= 0.97`); `medium` needs
`score >= 0.75`, `inlier >= 0.6`, and `psr >= 2.0` (or `score >= 0.90`); otherwise `low`.

## Ordering and the retry ladder

`core/order.py` turns pairwise scores into one chain:

- `build_overlap_matrix` scores every ordered pair `(i, j)`.
- `order_frames` links valid directed edges strongest-first into disjoint chains (each node has
  ≤1 successor and ≤1 predecessor; a union-find guards against cycles), joins leftover sub-chains
  by best available score (timestamps only break ties and seed the first pick), then calls
  `_validate_adjacencies` on the assembled order.

For each adjacency that is not already a valid matrix edge, `relaxed_vertical_offset` is tried at
ladder rungs 1→3:

| Rung | `ds` | window | `zncc_min` | `psr_min` | `inlier_min` |
|------|------|--------|-----------|-----------|--------------|
| 0 (default, in matrix) | 2 | 16 | 0.70 | 1.5 | 0.50 |
| 1 | 2 | 16 | 0.65 | 1.4 | 0.45 |
| 2 | 1 | 24 | 0.60 | 1.3 | 0.40 |
| 3 | 1 | 10000 (exhaustive) | 0.55 | 1.2 | 0.35 |

The first rung that validates **replaces the matrix entry** (so stitch and splice see the
overlap) and records a non-fatal `low`-confidence join. If no rung validates, the adjacency is
recorded as a **fatal gap**: `{"between": (a, b), "fatal": True, "reason": "no overlap anchor"}`.

## Fatal-gap semantics

A fatal gap means a section of the conversation is genuinely missing between two captures — no
amount of relaxation finds a shared anchor. `order_frames` never raises; it returns the gap with
`fatal: True` and lets the caller decide. `run_pipeline` collects fatal gaps into
`report.json:fatal_gaps` and still writes the partial output. The model must check that field
first and, if non-empty, stop and ask the user to re-capture the missing stretch with overlap
(see the SKILL flow). Adjacencies recovered only by relaxing are *non-fatal* — surface them as
low-confidence joins, not failures.

## Implementation note (CONTRACT deviation, flagged in code)

`OverlapResult.per_row_corr` is one value **per signature row**, so its length is `overlap // ds`
(signature space), not `overlap`. The contract text says `(overlap,)`. The module works entirely
in signature space, so this is the natural, non-fabricated output; `overlap` itself is still
returned in original content-row coordinates. `core/stitch.py:_resample_corr` resamples this
vector to content-row length before splice selection uses it. Likewise, the PSR is computed from
the ZNCC score curve rather than the coarse correlation the contract names — a deliberate,
documented choice because the coarse profile is too weak for a reliable ratio.
