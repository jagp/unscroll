# Video intake — scroll capture to clean keyframes

When the input is a screen recording of someone scrolling a chat, unscroll reduces it to the
minimal set of sharp, settled, mutually-overlapping still frames — the equivalent of a good set
of screenshots — which then flow through the identical screenshot pipeline. Implemented in
`core/video.py`. Design principle: **dumb extraction, smart selection.** ffmpeg dumps frames
cheaply; all the judgement happens in numpy.

`intake` routes any `.mp4/.mov/.m4v/.avi/.mkv/.webm` input to `video_to_frames`; the resulting
`Frame` objects carry `source="video"` and are indistinguishable to the rest of the pipeline.

## Pass 1 — dense, dumb extraction

`extract_candidates(video_path, workdir, fps=15, scale=0.5)` shells out to ffmpeg to dump a
dense, downscaled JPEG stream: `-vf "fps=15,scale=iw*0.5:ih*0.5"`, written as `cand_%06d.jpg`.
Every subprocess call passes an explicit timeout and checks the return code; a non-zero exit
raises with the tail of ffmpeg's stderr. Downscaling keeps selection fast — full resolution is
recovered only for the chosen frames in pass 2.

## Frame metrics

- **`scroll_sharpness(gray)`** — directional sharpness = variance of the vertical 2nd derivative,
  `var(g[2:] - 2*g[1:-1] + g[:-2])`. Scroll motion blur smears content along the vertical
  (scroll) axis, collapsing the vertical 2nd derivative; a low value means a blurred frame.
- **`inter_frame_shift(grayA, grayB)`** — signed vertical scroll displacement in pixels, from
  `coarse_lag_fft` on the row signatures in both directions (keeping whichever direction gives
  the larger overlap). **Positive = downward scroll** (content moved up); **negative = upward**,
  which is the signature of an inertial bounce.

## Selection (`select_keyframes`, `target_overlap=0.35`)

A five-step numpy pass turns the dense candidates into keepers:

1. **Per-frame metrics** — directional sharpness, signed inter-frame shift, and per-frame uniform
   (blank) band fraction.
2. **Dedup paused runs** — consecutive frames with shift ≈ 0 (within `_PAUSE_SHIFT_PX = 2` px)
   and near-identical luma (mean abs diff ≤ `_PAUSE_MAD = 3.0`) are one paused view; keep only
   the sharpest member.
3. **Reject motion blur** — drop any surviving frame whose sharpness is below `0.45 ×` the
   running median sharpness (an adaptive, local threshold).
4. **Reject trailing inertial bounce** — at the very end of the recording, a scroll often springs
   back: a contiguous run of trailing frames whose scroll sign reverses against the dominant
   direction, or that expose a large uniform blank band (≥ 30% of rows) scrolled past the content
   edge, is dropped. Only a run at the very end is rejected — a mid-stream direction change is a
   legitimate user scroll and is kept.
5. **Greedy overlap-constrained walk** — starting from the first good frame, advance to the
   *farthest* later good frame whose displacement from the last kept frame still leaves at least
   `target_overlap` (35%) of the frame height overlapping (displacement ≤ `(1 - overlap) × H`).
   Positions are the cumulative sum of the *adjacent* shifts (measuring the shift between two
   distant frames directly is unreliable once they no longer overlap). If even the next frame
   overshoots the budget, it is kept anyway and flagged as a coverage gap — coverage is never
   dropped and the routine never crashes.

`select_keyframes` returns the kept indices, or a `SelectionResult` (kept plus `dropped_blur` /
`dropped_pause` / `dropped_bounce` / `coverage_gaps` diagnostics) when `return_details=True`.
Everything degrades gracefully: if every frame is rejected, it falls back to the single sharpest
frame.

## Pass 2 — full-resolution re-extraction

`video_to_frames(video_path, workdir, fps=15, scale=0.5)` ties it together: extract candidates →
`select_keyframes` → for each chosen candidate, seek ffmpeg to `t = candidate_index / fps` and
decode a single full-resolution PNG (`key_%04d.png` under `<workdir>/keyframes/`). Each becomes a
`Frame` with `source="video"`, sequential `index`, and `ts = t`. A single unseekable timestamp is
skipped (puntable) rather than crashing intake. The returned frames are ready for chrome masking,
overlap, ordering, stitch, and segmentation exactly like screenshots — the overlap guarantee from
selection (≥ 35% frame overlap) is what lets `vertical_offset` stitch them.
