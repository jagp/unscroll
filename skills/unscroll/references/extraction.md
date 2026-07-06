# Extraction — segmentation, dedup, and the model's crop-reading role

How individual messages are carved out of the ordered frames, de-duplicated across overlaps, and
handed to the model as crops to read. Segmentation is **pure geometry** — it never reads glyph
text. Implemented in `core/segment.py`; the model does the reading. This is where the puntable
rules live.

## Message segmentation (`segment_messages`)

Within a frame's content slice, a row is **background** when almost all of it matches the modal
background color (per-channel tolerance 16; a row is foreground when more than 2% of it is
non-background). Contiguous runs of foreground rows, bounded by background gutters, become
message **bands**. Runs separated by fewer than 6 background rows are merged; runs shorter than
4px are dropped as noise.

Each `Band` records `top`/`bottom` (rows within the frame, `bottom` exclusive), a `sender`, a
`kind`, and `bbox` = `(x0, y0, x1, y1)` of the foreground bubble mass.

### Sender side (geometry)

Sender is inferred from the horizontal center-of-mass of the band's foreground mass:

- center-of-mass **left** of center (beyond a 6% dead-band) → `"other"`
- center-of-mass **right** of center → `"self"`
- centered, ambiguous, or **full-width** (bubble spans ≥ 90% of width, e.g. a system line) →
  `"unknown"`

This encodes the iOS-Messages convention (incoming left, outgoing right) without hardcoding it.
The model should trust `sender` unless a crop clearly contradicts it.

### Kind guess (puntable)

`_guess_kind` is a cheap shape heuristic that defaults to `"text"`. Only a large, densely filled,
smooth region (tall, ≥ 55% filled, low edge energy) is guessed as `"image"` — a text bubble,
however large, has high glyph edge energy and is excluded. Audio is intentionally **not** guessed:
a waveform heuristic misfires on ordinary short bubbles, and a false `[AUDIO MESSAGE]` is worse
than defaulting to text, which the model corrects when it reads the crop. Kind is puntable by
design; `segment_messages` never raises.

## Cross-overlap dedup (`dedup_bands`)

Walking the frames in `order`, each message must appear **once** even though it is visible near
the bottom of one frame and the top of the next. `dedup_bands` uses the `OverlapResult.overlap`
for each adjacency `(prev, cur)` to know cur's top overlap zone. A cur band whose top lies inside
that zone is a dedup candidate; it is matched against prev's already-emitted bands by:

- a strong downsampled-luma **signature correlation** (≥ 0.85) over the shared pixels, mapped
  from cur content rows into prev content rows, and
- the **same sender** covering at least 50% of the vertical span.

On a match, the duplicate is dropped and the **taller** (more complete) instance is kept — if cur
shows more of the message than prev did, the kept crop is upgraded in place. Each surviving
message is saved as a full-width high-res PNG crop under `<workdir>/crops/`
(`msg_XXXX_fN.png`), and returned as `{"frame": idx, "band": Band, "crop_png": path}`.

`run_pipeline` turns each dedup item into a canonical message record with everything the model
must fill starting `null`/`low`, and the crop path carried in `notes` as `crop:<path>`.

## The model's role — read the crops

The scripts get you to per-message crops; **you read them.** For each message, open the crop at
`notes:crop:<path>` and fill:

- **`content`** — the text of the message.
- **`display_name`** — the sender's visible name if shown, else `null`.
- **`type`** — correct the geometry's guess against what you see.
- **`confidence`** — `high`/`medium`/`low` for how readable the crop was.

## Puntable rules (do not guess)

- **Unreadable text** — leave `content` `null`, set `confidence: "low"`, add a note describing why
  (blurred, cropped, obscured). Never invent obscured or low-resolution text.
- **Media you can't identify** — set the best `type` you can, keep `content` `null`, add a note.
  An unclassified attachment stays a placeholder, not a fabricated description. Do not describe
  image contents unless the user explicitly asks.
- **Reactions** — if you can read the reaction, set `type: "reaction"` and put it in `content`;
  if not, punt (`null` + note).

## Timestamps and interpolation

- A **visible in-thread time** → set `timestamp` (ISO 8601) and `timestamp_source: "explicit"`.
- **Between two explicit anchors**, you may linearly interpolate a message's time; set
  `timestamp_source: "interpolated"`.
- **No signal** → leave `timestamp` `null` and `timestamp_source: "absent"`.

Timestamps are best-effort metadata, never the ordering authority — the sequence is already fixed
by visual overlap before you read a single crop.
