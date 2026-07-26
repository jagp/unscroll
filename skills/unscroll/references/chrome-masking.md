# Chrome masking — registry-free content-region detection

"Chrome" is the non-conversation UI: status bar, navigation/header bar, message input bar, and
on-screen keyboard. Before any overlap or segmentation work, each frame must be cropped to just
its message-content region, because chrome sits at fixed screen rows and would otherwise create
false "overlap" (identical status bars) or be mistaken for message content. Implemented in
`core/chrome.py`.

## No registry

Unscroll carries **no per-app or per-device profile**. There is no table of "iOS Messages status
bar = 47px". Chrome boundaries are discovered at runtime from the pixels themselves, so the skill
works on layouts it has never seen. Two methods do this.

## Method 1 — temporal-stability mask (strong signal)

`temporal_chrome_mask(grays)` takes a stack of same-resolution luma frames of the same app. The
insight: as the user scrolls, message content moves while chrome stays put, so chrome rows are
near-identical across frames and content rows change.

- Compute per-`(row, col)` variance across the stack, then average across columns to get a
  length-`H` per-row temporal-variance vector: **low over chrome, high over content**.
- Set an adaptive threshold just above the low (chrome) plateau: `floor + 0.10 * (peak - floor)`.
- The leading contiguous run of low-variance rows is **top chrome**; the trailing low-variance
  run is **bottom chrome**. Everything between is the content slice `(top, bottom)`.

Confidence is `high` when the chrome plateau is essentially flat relative to content (chrome
variance ≤ 5% of the content-row median and some chrome was actually stripped), `medium` when
≤ 25%, else `low`. With fewer than two frames, a zero-variance stack, or a degenerate
separation, it returns the whole frame at `low` confidence rather than guessing.

## Method 2 — structural fallback (single image)

`structural_content_slice(gray)` handles a single frame (or any resolution group too small for
the temporal method). Chrome bands are near constant-color horizontal runs, so a row is
"chrome-like" when it is **flat** (row variance below `max(5.0, 2% of the busiest row)`) **or**
**predominantly background** (over 90% of its pixels within tolerance of the modal luma). It
walks inward from the top and bottom while rows look like chrome, and returns the interval
between. Confidence is `medium` when it actually stripped something, else `low` — inherently
lower than the temporal method. If the whole image reads as chrome, it punts with the full frame.

## Orchestration

`content_slices(frames)` returns one `ContentSlice` per frame, in the original order:

1. Group frames by `(height, width)`.
2. A group with **≥ 3 frames** uses the temporal method; every frame in that group shares the
   single detected slice.
3. Smaller groups fall back to the structural method, per frame.

`ContentSlice` fields: `top`, `bottom` (exclusive), `method` (`"temporal"` | `"structural"`),
and `confidence` (`"high"` | `"medium"` | `"low"`). Rows increase downward. Downstream stages
(`offset`, `stitch`, `segment`) always operate inside `[top, bottom)`, never on raw frame rows,
so chrome never pollutes overlap scores or message crops.
