---
name: unscroll
description: >
  Reconstructs a complete text-message conversation thread into a structured, portable
  transcript from EITHER overlapping screenshots OR a scroll-capture video. Use this skill
  whenever a user provides screenshots of a messaging conversation — uploaded as individual
  image files, a folder, or a ZIP — or a screen recording of a chat, and wants them assembled
  into a transcript, archive, or structured record. Triggers on: "stitch my screenshots",
  "combine these messages", "reconstruct my conversation", "make an archive of my texts",
  "export my chat history from screenshots", "video of me scrolling my messages", "screen
  recording of a chat", "turn this scroll capture into a transcript", or any request to
  assemble fragmented or scrolled views of a messaging thread into a readable whole. Also
  triggers when a user provides a batch of conversation screenshots or a scroll video without
  explicit instructions, clearly intending to do something with them together. Handles iOS
  Messages and other common chat layouts; chrome (status bar, nav, input bar, keyboard) is
  detected at runtime, so no per-app configuration is needed.
---

# Unscroll

Reconstruct a chat thread into a structured transcript from **overlapping screenshots** or a
**scroll-capture video**. Image-only: unscroll reads what is on screen, never a device or a
message database.

The work splits cleanly. The scripts do **all** the geometry — overlap detection, ordering,
stitching, segmentation, dedup, video keyframe selection — and **no text recognition
whatsoever**. There is no OCR in this tool. **You read the glyph text** from the per-message
crops the scripts emit and fill it into the transcript. Without that step the transcript has
no content at all.

The stitched `validation.png` is a **completeness check**, not the deliverable. Its job is to
let you and the user confirm the capture is gapless. The transcripts are the deliverable.

## Puntable by design

Media, timestamps, and reactions are best-effort. When something can't be read, set it to
`null`, mark `confidence: "low"`, add a short note, and **keep going**. Never guess obscured
or low-resolution text. The **only** fatal condition is an unrecoverable overlap gap — a
genuinely missing section of the capture.

## Environment

Python 3.14 with `numpy` and `Pillow`. `ffmpeg` is needed only for video input and HEIC
decode. Run the CLI through the **Bash tool** so `${CLAUDE_PLUGIN_ROOT}` and forward-slash
paths resolve; don't import the modules yourself.

## Orchestration flow

### 1. Identify the input

Determine whether the user gave you a folder/ZIP of screenshots, a single image, or a video
(`.mp4`, `.mov`, `.m4v`, `.avi`, `.mkv`, `.webm`). Don't sort or inspect the screenshots
first — the pipeline orders them by visual overlap and filenames are opaque.

### 2. Run the pipeline

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/unscroll/scripts/unscroll.py" run <input> --workdir <workdir> [--platform "iOS Messages"]
```

`--platform` is a metadata label only — it does not affect detection. Also available:
`--no-mark` (omit seam hairlines in the validation image) and `--no-timestamps` (ignore file
timestamps when ordering). The command prints a JSON summary and writes into `<workdir>`:

| Artifact | What it is |
|----------|------------|
| `report.json` | Confidence, flags, resolved order, fatal gaps |
| `validation.png` | The stitched thread — a completeness check |
| `crops/msg_*.png` | One crop per message, in order |
| `transcript.json` | The canonical document (`content` is `null` until you fill it) |
| `transcript.txt` / `.md` | Draft renders (unread messages show `[unreadable]`) |

### 3. Check for fatal gaps

Open `<workdir>/report.json`. If `fatal_gaps` is non-empty, **stop** and tell the user which
frames fail to overlap (the `between` field), for example:

> Reconstruction can't complete: no overlap was found between two of your captures, so a
> section of the conversation is missing between them. Please re-capture that stretch — make
> sure at least one message is visible in both the screenshot before the gap and the one
> after — and run it again.

Partial output is still written, but do not present it as complete.

### 4. Confirm completeness visually

Open `<workdir>/validation.png` and confirm the thread reads as one continuous conversation.
For a long thread the image will be extremely tall; mention that to the user.

### 5. Read the crops and fill the transcript

Open `<workdir>/transcript.json`. Each message's `notes` field carries `crop:<path>`. Open
that crop and read it. Fill in:

- `content` — the message text. Unreadable → leave `null`, set `confidence: "low"`, add a
  note. Do **not** guess obscured text.
- `display_name` — the sender's visible name if shown, else `null`. The geometry never sets
  this.
- `timestamp` — a visible in-thread time as ISO 8601, with `timestamp_source: "explicit"`.
  Between two known times you may interpolate and use `"interpolated"`. Otherwise leave it
  `null` with `"absent"`.
- `type` — the geometry only ever guesses `text` or `image`. Correct it to `audio`, `video`,
  `sticker`, `reaction`, `link_preview`, or `system` when the crop shows one.
- `confidence` — `high`/`medium`/`low`, reflecting how readable the crop was.

`sender` (`self`/`other`/`unknown`) comes from bubble side; override it only if the crop
clearly contradicts it.

### 6. Render the final transcripts

Save the filled JSON, then:

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/unscroll/scripts/unscroll.py" render <filled.json> --workdir <workdir> --formats json,text,markdown
```

`render` validates the schema first and refuses a malformed document, printing the specific
problems and exiting `1`.

### 7. Report to the user

Total messages; whether frames were reordered (`reordered`); overall confidence; any flags;
and the output paths.

## What the scripts guarantee (so you don't re-derive it)

- **Ordering is by content.** Capture order is verified, never trusted: consecutive pairs are
  checked first (O(n)); a shuffled batch escalates to a full pairwise overlap matrix and a
  greedy chain. Timestamps are only a tiebreaker. `ordering_strategy` in the report tells you
  which path ran (`chain`, `chain-reversed`, or `matrix-fallback`).
- **Gaps are retried before they're fatal.** Every adjacency that fails at the default
  threshold goes through a relaxation ladder first. Cheap invariant self-tests (stitched
  height vs. summed segments, monotonic indices) add flags rather than trusting bad output.
- **Registry-free chrome.** Chrome is detected per run from temporal stability across
  same-resolution frames, with a single-image structural fallback. No device or app profile.
- **Video becomes clean keyframes.** A recording is reduced to the minimal set of sharp,
  settled, overlapping stills, which then flow through the identical screenshot path.

## Reference files

Load these only when you need the detail; the flow above covers a normal run.

| File | Read when |
|------|-----------|
| `references/pipeline.md` | You need the end-to-end stages, CLI, and artifacts |
| `references/overlap-detection.md` | You need overlap scoring, confidence gates, or retry-ladder semantics |
| `references/chrome-masking.md` | You need the chrome detection detail |
| `references/video-intake.md` | The input is a video and you want keyframe-selection detail |
| `references/extraction.md` | You need segmentation, sender-side, dedup, or punting rules |
| `references/output-formats.md` | You need the canonical JSON schema or the text/Markdown formats |
