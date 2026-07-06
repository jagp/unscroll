# unscroll scripts — interface contract (authoritative)

Every module in `scripts/core/`, `scripts/formats/`, and `scripts/unscroll.py` MUST conform to the
signatures, data shapes, and conventions below. This file is the single source of truth for how the
modules integrate. If you think a signature needs to change, change it HERE first (in your returned notes)
and flag it — do not silently diverge.

## Global conventions

- Python 3.14, cross-platform (dev host is Windows). Standard library + **numpy** + **Pillow** ONLY.
  **No OpenCV / cv2, no torch, no scipy, no pytest.** ffmpeg is invoked as an external binary via
  `subprocess` (never a python binding).
- Images in memory: **RGB uint8** arrays of shape `(H, W, 3)`. Grayscale/luma: **float32** `(H, W)`,
  range 0–255.
- Tests use the stdlib **`unittest`** module, runnable via `python -m unittest`. No third-party test deps.
- Every public function has a one-line docstring and type hints. Keep modules importable in isolation
  (guard any `__main__` demo behind `if __name__ == "__main__":`).
- **Never raise for "puntable" problems** (unreadable text, missing timestamp, unknown media, low
  confidence): return the partial result with a `low` confidence flag and a note. The ONLY fatal condition
  is an unrecoverable overlap gap AFTER the retry ladder is exhausted (see `order`/`report`).
- Import style within the package: `from core import offset` / `from core.offset import vertical_offset`
  (scripts run with `scripts/` on `sys.path`; `unscroll.py` sets that up).

## Coordinate & overlap semantics (READ CAREFULLY — integration depends on this)

- Rows increase downward (row 0 = top). A "content slice" is `(top, bottom)` row indices bounding the
  message-content region after chrome (status bar / nav / input bar / keyboard) is masked off.
- `vertical_offset(grayA, grayB)` returns `overlap` = an integer number of rows such that
  **the last `overlap` rows of A equal the first `overlap` rows of B**:
  `A[H_A - overlap : H_A]  ≈  B[0 : overlap]`.
  A and B are assumed already cropped to their content slices before being passed in.
- Splice: within the overlap band, a cut row `s ∈ [0, overlap)` means A contributes rows
  `[0 : H_A - overlap + s]` and B contributes rows `[s : H_B]`. This yields a seamless join with no
  duplicated or dropped rows.

## Shared data types (use `@dataclass`; define in the module that owns them)

```python
# core/imageio.py
@dataclass
class Frame:
    id: str            # 16-hex content hash of the normalized RGB bytes
    path: str          # source path on disk (normalized PNG)
    rgb: np.ndarray    # (H,W,3) uint8
    gray: np.ndarray   # (H,W) float32
    width: int
    height: int
    source: str        # "screenshot" | "video"
    index: int         # original intake index (opaque; NOT trusted for order)
    ts: float | None   # filesystem/frame timestamp (epoch seconds) or None

# core/chrome.py
@dataclass
class ContentSlice:
    top: int
    bottom: int        # exclusive
    method: str        # "temporal" | "structural"
    confidence: str    # "high" | "medium" | "low"

# core/offset.py
@dataclass
class OverlapResult:
    overlap: int           # shared rows (see semantics above); 0 if none
    score: float           # median per-row ZNCC over content rows, [-1,1]
    psr: float             # peak-to-sidelobe ratio of the coarse correlation
    inlier: float          # fraction of content rows with per-row corr > 0.7
    valid: bool            # passed the valid-overlap test
    confidence: str        # "high" | "medium" | "low" | "none"
    per_row_corr: np.ndarray | None   # (overlap,) float32, or None

# core/splice.py
@dataclass
class SpliceResult:
    s: int             # cut row within [0, overlap)
    confidence: str    # "high" | "medium" | "low"
    band_width: int    # width of the gutter band the cut sits in (px)

# core/segment.py
@dataclass
class Band:
    top: int; bottom: int          # rows within the frame
    sender: str                    # "self" | "other" | "unknown"
    kind: str                      # "text" | "image" | "audio" | "video" | "sticker" | "reaction" | "system" | "unknown"
    bbox: tuple                    # (x0,y0,x1,y1) of the foreground bubble mass
```

## Module APIs

### core/imageio.py
- `load_image(path: str) -> np.ndarray` — decode to RGB uint8. PNG/JPG/JPEG/WEBP/BMP via Pillow; HEIC/HEIF
  via `ffmpeg -i <path> <tmp>.png` shell-out (Pillow can't). Raise `ValueError` only if truly undecodable.
- `to_gray(rgb: np.ndarray) -> np.ndarray` — float32 luma `(H,W)`.
- `content_hash(x: bytes | np.ndarray) -> str` — 16-char sha256 hex.
- `modal_color(rgb: np.ndarray) -> np.ndarray` — shape `(3,)` uint8 modal/background color (8-bit-binned
  histogram mode). Shared by splice + segment.
- `make_frame(path, source, index, ts=None) -> Frame`.
- `save_png(rgb: np.ndarray, path: str) -> None`.

### core/offset.py  (THE primitive; highest scrutiny)
- `row_signature(img: np.ndarray, ds: int = 2, K: int = 48) -> np.ndarray` — `(H//ds, K)` block-mean
  signature on a downsampled luma image.
- `coarse_lag_fft(sigA: np.ndarray, sigB: np.ndarray) -> int` — FFT cross-correlation peak lag of the
  column-collapsed profiles; nominates a candidate overlap.
- `vertical_offset(grayA, grayB, min_overlap=8, max_overlap=None) -> OverlapResult` — see semantics.
  Coarse FFT nominate → evaluate per-row ZNCC in a window → score. Module constants:
  `ZNCC_MIN = 0.70`, `PSR_MIN = 1.5`, `INLIER_MIN = 0.5`. `valid` iff
  `score>=ZNCC_MIN and psr>=PSR_MIN and inlier>=INLIER_MIN and overlap>=min_overlap`.
- `relaxed_vertical_offset(grayA, grayB, level: int) -> OverlapResult` — retry ladder rung `level`
  (0=default; higher = less downsample / wider window / relaxed thresholds). Used by the order retry loop.

### core/chrome.py
- `temporal_chrome_mask(grays: list[np.ndarray]) -> ContentSlice` — for a stack of SAME-resolution luma
  frames: per-row temporal variance; leading low-variance run = top chrome, trailing = bottom chrome.
- `structural_content_slice(gray: np.ndarray) -> ContentSlice` — single-image fallback (row variance +
  modal-bg fraction walk from both ends).
- `content_slices(frames: list[Frame]) -> list[ContentSlice]` — orchestrator: group by resolution; use
  temporal when a group has ≥3 frames, else structural. Returns one slice per input frame, in order.

### core/splice.py
- `gutter_profile(content_rgb: np.ndarray, bg: np.ndarray) -> tuple[np.ndarray, np.ndarray]` — returns
  `(bg_frac, edge)` per-row arrays.
- `select_splice_row(content_rgb: np.ndarray, overlap: int, per_row_corr: np.ndarray | None) -> SpliceResult`
  — choose a gutter cut `s ∈ [0, overlap)` near the overlap midpoint. Never fails; worst case returns the
  min-edge row with `low` confidence.

### core/order.py
- `build_overlap_matrix(frames, slices) -> dict[tuple[int,int], OverlapResult]` — directed pair scores
  (bottom-of-i vs top-of-j) for candidate pairs.
- `order_frames(frames, slices, matrix, use_ts=True) -> dict` — returns
  `{"order": list[int], "gaps": list[dict], "confidence": str, "reordered": bool, "matrix": ...}`. Greedy
  best-successor/predecessor chain; timestamps only break ties. Runs the **retry ladder** on any failing
  adjacency before declaring a fatal gap. A fatal gap is reported in `gaps` with `fatal: True` (the caller
  decides whether to halt).

### core/stitch.py
- `stitch(frames, order, slices, splices, mark=True) -> np.ndarray` — assemble the validation RGB image;
  1px hairline splice markers when `mark`. `splices` maps adjacency → SpliceResult.

### core/segment.py
- `segment_messages(frame: Frame, slice: ContentSlice, bg: np.ndarray) -> list[Band]` — contiguous
  non-background row runs; sender via foreground column-mass (left→"other", right→"self", ambiguous →
  "unknown"); cheap `kind` shape-guess (puntable). 
- `dedup_bands(frames, order, slices, overlaps, bands_by_frame) -> list[dict]` — ordered, de-duplicated
  message list; each item: `{"frame": idx, "band": Band, "crop_png": path}` where `crop_png` is a saved
  high-res crop for the model to read. Messages recurring in an overlap zone appear once.

### core/video.py
- `extract_candidates(video_path, workdir, fps=15, scale=0.5) -> list[str]` — ffmpeg pass-1 frame paths.
- `scroll_sharpness(gray: np.ndarray) -> float` — vertical 2nd-derivative variance (motion-blur metric).
- `select_keyframes(candidate_paths, target_overlap=0.35) -> list[int]` — indices of sharp, settled,
  mutually-overlapping frames (dedup pauses, reject inertial bounce, greedy overlap-constrained). Uses
  `offset.coarse_lag_fft` for inter-frame shift.
- `video_to_frames(video_path, workdir) -> list[Frame]` — pass-1 candidates → select → pass-2 full-res
  re-extract of chosen timestamps → `Frame` list (source="video"), ready for the screenshot pipeline.

### core/checkpoint.py
- `class Manifest` with `load(workdir)`, `save()`, `cached(stage: str, key: str) -> str | None`,
  `record(stage: str, key: str, output_ref: str)`. Stage results keyed by a content hash of inputs+params
  so reruns are idempotent/resumable.

### core/report.py
- `rollup(order_result, overlaps, splices, messages, platform_hint=None) -> dict` — the metadata block.
- `invariants(stitched, segments, messages) -> list[str]` — cheap self-tests (stitched height ≈ Σ segment
  heights; monotonic message count; no duplicate text straddling a boundary). Returns a list of violated-
  invariant flags (empty = clean); callers downgrade confidence rather than trusting bad output.

### formats/  (all render FROM the canonical JSON document)
The canonical in-memory document is `{"metadata": {...}, "messages": [ {message}, ... ]}`.
- `json_out.write_json(doc: dict, path: str) -> None`.
- `text_out.write_text(doc: dict, path: str) -> None` — flat `HH:MM · NAME` / `  content` lines.
- `markdown_out.write_markdown(doc: dict, path: str) -> None` — dialogue-script format: page header,
  `── DATE ──` section headers, `── [N-hour/N-day gap] ──` markers, `[IMAGE]`/`[AUDIO MESSAGE]`/`[VIDEO]`/
  `[STICKER]`/`[LINK]` media placeholders, pagination (new page at month boundary OR 150 messages, break
  at a natural gap). Reuse the original draft spec in `skills/unscroll/SKILL.md` history / references.

## Canonical JSON schema

```json
{
  "metadata": {
    "skill": "unscroll", "platform": "string|null", "platform_confidence": "high|medium|low",
    "participants": 2, "date_range": {"start": "ISO8601|null", "end": "ISO8601|null"},
    "total_messages": 0, "sources_used": 0, "source_kind": "screenshots|video",
    "stitch_confidence": "high|medium|low", "flags": [], "generated_at": "ISO8601"
  },
  "messages": [
    {
      "index": 0,
      "sender": "self|other|unknown",
      "display_name": "string|null",
      "timestamp": "ISO8601|null",
      "timestamp_source": "explicit|interpolated|absent",
      "content": "string|null",
      "type": "text|image|audio|video|sticker|reaction|link_preview|system",
      "confidence": "high|medium|low",
      "notes": "string|null"
    }
  ]
}
```

## `unscroll.py` CLI (orchestrator)
Subcommands, each resumable via the checkpoint manifest, `--workdir` shared:
`intake`, `chrome`, `overlap`, `order`, `stitch`, `segment`, `video`, `render`, and `run` (full pipeline:
intake→chrome→overlap→order→stitch→segment→[model reads crops externally]→render). `run` on a directory of
images does the screenshot path; `run` on a video file runs `video_to_frames` first. Emits a `report.json`
summarizing confidence + flags. Text output only (no prints on the happy path except a final summary).
