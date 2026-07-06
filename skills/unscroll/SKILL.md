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

Reconstruct an asynchronous chat thread (iOS-Messages-style, or generic) into a structured
transcript from **overlapping screenshots** or a **scroll-capture video**. Image-only: unscroll
never touches a device or a message database — it reads what is on screen.

The work splits cleanly. Deterministic scripts (numpy/Pillow/ffmpeg) do ALL the geometry:
overlap detection, ordering, stitching, message segmentation, dedup, and video keyframe
selection. **You (the multimodal model) read the glyph text** from the per-message crop images
the scripts emit, and fill it into the transcript. Optional OCR (tesseract) is used only if the
binary is present; it is never required.

The stitched `validation.png` is a **completeness check**, not the deliverable. Its only job is
to let you and the user confirm the capture is gapless. The deliverables are the transcript
files (JSON, plain text, Markdown).

## Puntable by design

Media, timestamps, and reactions are best-effort. When something cannot be read, set it to
`null`, mark `confidence: "low"`, add a short note, and **keep going**. Never guess obscured or
low-resolution text. The **only** fatal condition is an unrecoverable overlap gap after the
retry ladder is exhausted — a genuinely missing section of the capture.

## Environment

- Python 3.14+, `numpy`, `Pillow`, and `ffmpeg` (the system binary — used for HEIC decode and
  video frame extraction). `tesseract` is optional.
- No OpenCV. Scripts are invoked through the CLI below; do not import the modules yourself.
- Prefer running the CLI through the **Bash tool** so `${CLAUDE_PLUGIN_ROOT}` and forward-slash
  paths resolve correctly.

## Orchestration flow

Follow these steps in order.

### 1. Identify the input

Determine whether the user gave you a folder/ZIP of screenshots, a single image, or a video
file (`.mp4`, `.mov`, `.m4v`, `.avi`, `.mkv`, `.webm`). You do not need to sort screenshots or
inspect them first — the pipeline orders them by visual overlap. File names are opaque.

### 2. Run the pipeline

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/unscroll/scripts/unscroll.py" run <input> --workdir <workdir> [--platform "iOS Messages"]
```

`<input>` is the folder, ZIP, image, or video path. `<workdir>` is a fresh working directory
for artifacts. `--platform` is an optional label recorded in metadata (it does not change
detection — chrome is found at runtime). Other flags: `--no-mark` (omit splice hairlines in the
validation image), `--no-timestamps` (ignore file timestamps entirely when ordering).

The command prints a JSON summary and writes into `<workdir>`:

| Artifact | What it is |
|----------|------------|
| `report.json` | Confidence, flags, resolved order, and any fatal gaps |
| `validation.png` | The stitched thread — a completeness check |
| `crops/msg_*.png` | One high-res crop per message, in order |
| `transcript.json` | The canonical document (`content` is `null` until you read it) |
| `transcript.txt` / `transcript.md` | Draft renders (unread messages show `[unreadable]`) |

### 3. Check for fatal gaps

Open `<workdir>/report.json`. If `fatal_gaps` is **non-empty**, stop and tell the user exactly
which frames fail to overlap. A fatal gap means a section of the conversation is missing between
two captures. Report it plainly, for example:

> Reconstruction can't complete: no overlap was found between two of your captures, so a section
> of the conversation is missing between them. Please re-capture that stretch — make sure at
> least one message is visible in both the screenshot before the gap and the one after — and run
> it again.

Include the specific frame pair from the `fatal_gaps` entry (its `between` field). The pipeline
still writes partial output, but do not present it as complete. If `fatal_gaps` is empty,
continue.

### 4. Confirm completeness visually

Open `<workdir>/validation.png` and confirm the thread reads as one continuous, gapless
conversation. This is a sanity check on the stitch, not the output you deliver. For a very long
thread the image will be extremely tall; note that to the user.

### 5. Read the crops and fill the transcript

Open `<workdir>/transcript.json`. For each message, the `notes` field carries `crop:<path>`.
Open that crop image and read it. For each message, fill in:

- `content` — the message text. Unreadable → leave `null`, set `confidence: "low"`, add a note.
  Do NOT guess obscured text.
- `display_name` — the sender's visible name, if shown; else leave `null`.
- `timestamp` — a visible in-thread time as ISO 8601, and set `timestamp_source` to `"explicit"`.
  Between two known times you may interpolate and set `"interpolated"`. Otherwise leave
  `timestamp` `null` and `timestamp_source` `"absent"`.
- `type` — correct it if the geometry guessed wrong (`text`, `image`, `audio`, `video`,
  `sticker`, `reaction`, `link_preview`, `system`).
- `confidence` — bump to `high`/`medium`/`low` to reflect how readable the crop was.

Punt on anything you can't identify: media you can't classify, a reaction you can't read, a
timestamp that isn't shown — keep it `null`, add a note, move on. The `sender` field
(`self`/`other`/`unknown`) is set by geometry; only override it if the crop clearly contradicts
it.

### 6. Render the final transcripts

Save the filled JSON, then run:

```bash
python "${CLAUDE_PLUGIN_ROOT}/skills/unscroll/scripts/unscroll.py" render <filled.json> --workdir <workdir> --formats json,text,markdown
```

`render` validates the document schema first and refuses to render a malformed one (it prints
the specific problems). It writes `transcript.json`, `transcript.txt`, and `transcript.md`.

### 7. Report to the user

Summarize: total messages; whether frames were reordered (`reordered` in the report); overall
confidence; any flags (low-confidence joins, unreadable messages, invariant warnings); and the
output file paths.

## What the scripts guarantee (so you don't re-derive it)

- **Ordering is by content, not file order.** Screenshots arrive in any order; a full pairwise
  overlap matrix and a greedy chain reconstruct the true top-to-bottom sequence. Timestamps are
  only a tiebreaker.
- **Self-healing.** Work is content-hash checkpointed (resumable). Every adjacency that doesn't
  validate at the default threshold is retried through a relaxation ladder before any gap is
  called fatal. Cheap invariant self-tests (stitched height vs. summed segments, monotonic
  indices, no duplicate content across a seam) downgrade confidence rather than trust bad output.
- **Registry-free chrome.** Status bar / nav / input bar / keyboard are detected per run from
  temporal stability across same-resolution frames, with a single-image structural fallback. No
  device or app profile is needed.
- **Video becomes clean keyframes.** A scroll recording is reduced to the minimal set of sharp,
  settled, mutually-overlapping stills, which then flow through the identical screenshot path.

## Reference files

Load these only when you need the detail; the flow above is enough for a normal run.

| File | Read when |
|------|-----------|
| `references/pipeline.md` | You need the end-to-end stages, CLI, artifacts, and self-healing tactics |
| `references/overlap-detection.md` | You need overlap scoring, confidence gates, or fatal-gap / retry-ladder semantics |
| `references/chrome-masking.md` | You need the registry-free chrome detection detail |
| `references/video-intake.md` | The input is a video and you want the keyframe-selection detail |
| `references/extraction.md` | You need segmentation, sender-side, dedup, or the puntable media/timestamp rules |
| `references/output-formats.md` | You need the canonical JSON schema or the text/Markdown transcript formats |
