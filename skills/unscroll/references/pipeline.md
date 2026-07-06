# Pipeline — stages, CLI, artifacts, self-healing

The end-to-end reconstruction and the two CLI entry points that drive it. This is the map;
the per-stage references (`overlap-detection.md`, `chrome-masking.md`, `video-intake.md`,
`extraction.md`, `output-formats.md`) hold the detail.

## The two commands

The orchestrator is `skills/unscroll/scripts/unscroll.py`. It exposes exactly two subcommands.

### `run <input> --workdir <wd>`

Runs the whole geometry pipeline and emits the crops the model reads.

```
python "${CLAUDE_PLUGIN_ROOT}/skills/unscroll/scripts/unscroll.py" run <input> --workdir <wd> [--platform "iOS Messages"] [--no-mark] [--no-timestamps]
```

- `<input>` — a folder of screenshots, a `.zip` of them, a single image, or a video file.
- `--platform` — a free-text label stored in metadata. It does not steer detection.
- `--no-mark` — omit the 1px splice hairlines in `validation.png`.
- `--no-timestamps` — ignore file timestamps entirely during ordering (pure content order).

Exit code is `2` when one or more overlap gaps could not be bridged (partial output is still
written); `0` otherwise.

### `render <doc.json> --workdir <wd> --formats json,text,markdown`

Renders final transcripts from a completed canonical document. It first runs the schema
validator and refuses to render a malformed document, printing the specific problems. `--workdir`
defaults to the document's own directory; `--formats` defaults to `json,text,markdown`.

## Stages inside `run`

`run` calls `run_pipeline()`, which executes these stages in order:

1. **Intake** (`unscroll.py:intake`) — resolve the input to a list of `Frame` objects plus a
   `source_kind` of `screenshots` or `video`. A folder is listed by `(mtime, name)`; a `.zip` is
   extracted and walked for images; a single image becomes one frame; a video is handed to
   `core.video.video_to_frames` first (see `video-intake.md`). Corrupt images are skipped with a
   warning rather than sinking the batch. File names are opaque — order here is a weak prior only.

2. **Chrome mask** (`core.chrome.content_slices`) — per frame, compute the `ContentSlice`
   `(top, bottom)` that bounds the message-content region after status bar / nav / input bar /
   keyboard are masked off. Registry-free; see `chrome-masking.md`.

3. **Overlap matrix** (`core.order.build_overlap_matrix`) — score every ordered pair `(i, j)`
   (bottom of `i` vs top of `j`) with `core.offset.vertical_offset`. `N` is small, so the full
   directed matrix is computed. See `overlap-detection.md`.

4. **Ordering** (`core.order.order_frames`) — greedily assemble the true top-to-bottom chain from
   the valid directed edges, join leftover sub-chains, then run the **retry ladder** on every
   adjacency that didn't already validate. Returns `order`, `gaps`, `confidence`, `reordered`,
   and the (possibly updated) `matrix`. Fatal gaps are flagged here but never raised.

5. **Stitch** (`core.stitch.compute_splices` + `stitch`) — choose a gutter splice row inside each
   overlap band, then concatenate each frame's content contribution into one tall RGB image,
   `validation.png`. The splice math duplicates and drops no rows. 1px neutral hairlines mark the
   seams unless `--no-mark`.

6. **Segment + dedup** (`core.segment.segment_messages` + `dedup_bands`) — carve each content
   region into message bands, then walk the ordered frames emitting each message once, dropping
   the copies that recur inside overlap zones. Each surviving message is saved as a high-res crop
   under `<wd>/crops/`. See `extraction.md`.

7. **Metadata + invariants** (`core.report.rollup` + `invariants`) — build the canonical
   `metadata` block and run cheap self-tests; violations are appended to `metadata.flags`.

8. **Write** — render draft transcripts (content still unread), then write `report.json`.

The model then reads the crops, fills the JSON, and calls `render` (see the SKILL flow).

## Artifacts written to `<workdir>`

| Path | Produced by | Purpose |
|------|-------------|---------|
| `report.json` | `run` | Summary: input, `source_kind`, `frames`, `order`, `reordered`, `confidence`, `total_messages`, `fatal_gaps`, `flags`, and artifact paths |
| `validation.png` | stitch | Completeness check — the stitched thread (may be very tall) |
| `crops/msg_XXXX_fN.png` | dedup | One high-res crop per message, in order (`N` = source frame index) |
| `transcript.json` | render | Canonical document; `content` is `null` until the model fills it |
| `transcript.txt` | render | Flat plain-text draft |
| `transcript.md` | render | Paginated dialogue-script draft |
| `manifest.json` | checkpoint | Resumable stage cache (see below) |
| `video/`, `keyframes/` | video intake | Pass-1 candidate frames and chosen full-res keyframes (video input only) |

`report.json` is the file to read first: check `fatal_gaps` before anything else.

## Self-healing tactics

Unscroll is built to degrade gracefully and never trust bad output.

- **Content-hash checkpointing** (`core.checkpoint.Manifest`) — stage results are keyed by a
  16-char hash of their inputs+params, persisted atomically as `manifest.json`. A re-run in the
  same workdir is idempotent and resumable; a corrupt manifest is treated as absent, not fatal.
- **Full pairwise overlap matrix** — ordering sees every candidate edge, not just adjacent file
  pairs, so a shuffled batch still reconstructs correctly.
- **Content-based ordering** — the sequence is driven by visual overlap; file/frame timestamps
  are only a tiebreaker and a stable seed for a fully-disconnected batch. `--no-timestamps`
  removes even that.
- **Per-adjacency retry ladder** — an adjacency that fails the default overlap gates is retried
  through progressively relaxed rungs (finer resolution, wider search window, looser thresholds)
  before it is ever declared a fatal gap. Recovered adjacencies become non-fatal low-confidence
  joins. See `overlap-detection.md`.
- **Graceful degradation (puntable)** — unreadable text, missing timestamps, and unknown media
  never raise. They become `null` + `low` confidence + a note, and the pipeline continues. The
  only fatal condition is an unrecoverable overlap gap after the ladder is exhausted, and even
  then the partial result is written.
- **Invariant self-tests** (`core.report.invariants`) — cheap checks (stitched height ≈ sum of
  segment heights; strictly monotonic message indices; no identical content in adjacent
  messages) return the names of violated invariants. Callers append these to `flags` and
  downgrade confidence rather than presenting suspect output as trustworthy.
