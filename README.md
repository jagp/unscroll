# unscroll

Reconstruct an asynchronous text-message thread into a structured, portable transcript — from
either **overlapping screenshots** or a **scroll-capture video** of a chat. unscroll is a
[Claude Code](https://claude.com/claude-code) plugin: a model-facing skill plus a set of
deterministic Python scripts.

It handles iOS-Messages-style layouts and generic two-party chats. There is no per-app or
per-device configuration — the on-screen chrome (status bar, nav bar, input bar, keyboard) is
detected at runtime, so it works on layouts it has never seen.

## What it does

Given a pile of partial screenshots (in any order) or a screen recording of you scrolling a
conversation, unscroll:

1. Detects and masks the UI chrome on every frame.
2. Finds where frames overlap and reconstructs the true top-to-bottom order (by image content,
   not file names or timestamps).
3. Stitches a tall `validation.png` so you can confirm the capture is complete and gapless.
4. Segments each message, de-duplicates the copies that recur in overlaps, and emits a high-res
   crop per message.
5. Has the model read those crops and fill in the text, then renders the transcript as **JSON**,
   **plain text**, and a paginated **Markdown dialogue script**.

The split is deliberate: the scripts do all the geometry deterministically; the multimodal model
only reads glyphs from the crops. The stitched image is a completeness check — the transcript
files are the deliverable.

## How it works

- **Content-based ordering** — a full pairwise overlap matrix plus a greedy chain recover the
  order even from a shuffled batch; timestamps are only a tiebreaker.
- **Registry-free chrome detection** — a temporal-stability mask (per-row variance across
  same-resolution frames) finds the content region, with a single-image structural fallback.
- **Self-healing** — content-hash checkpointing (resumable runs), a per-adjacency retry ladder
  before any gap is called fatal, and invariant self-tests that downgrade confidence rather than
  trust bad output.
- **Puntable by design** — unreadable media, timestamps, and reactions become `null` + a
  low-confidence note and the pipeline continues. The only fatal condition is a genuinely missing
  section of the capture (no overlap between two frames after the retry ladder).

## Requirements

- **Python 3.14+**
- **numpy** and **Pillow**
- **ffmpeg** (system binary) — used for HEIC decode and video frame extraction
- **tesseract** — optional; used only if present, never required

No OpenCV, no torch, no scipy.

## Install

From within Claude Code, add this repository as a plugin marketplace and install:

```
/plugin marketplace add jagp/unscroll
/plugin install unscroll
```

For local development, point Claude Code at your working copy:

```
claude --plugin-dir /path/to/unscroll
```

## Quickstart

Just give Claude your screenshots or scroll video and ask it to reconstruct the conversation —
the skill triggers on requests like "stitch these screenshots into a transcript" or "turn this
screen recording of my chat into a transcript." Under the hood it runs:

```bash
# 1. Run the geometry pipeline (folder of screenshots, a .zip, an image, or a video)
python "${CLAUDE_PLUGIN_ROOT}/skills/unscroll/scripts/unscroll.py" run <input> --workdir <workdir>

# 2. Claude reads <workdir>/crops/*.png, fills <workdir>/transcript.json,
#    then renders the final transcripts:
python "${CLAUDE_PLUGIN_ROOT}/skills/unscroll/scripts/unscroll.py" render <filled.json> --workdir <workdir> --formats json,text,markdown
```

Outputs land in `<workdir>`: `report.json`, `validation.png`, `crops/`, and
`transcript.json` / `.txt` / `.md`.

## Limitations

- **Overlap is required.** Adjacent captures must share at least one message. If a stretch of the
  conversation was never captured, unscroll reports the exact frame pair and stops — it will not
  fabricate the missing section.
- **Two-party threads** are the primary target. Group threads work with reduced sender-ID
  confidence.
- **Best-effort metadata.** Timestamps, media types, and reactions are filled only when legibly
  visible; otherwise they are left null and flagged.
- **Text quality is bounded by the capture.** Blurry, tiny, or obscured text is left unread rather
  than guessed.
- **Sender attribution** is inferred from bubble side (left/right) and can be `unknown` for
  centered or full-width content.

## Privacy and scope

- **Image-only.** unscroll reads only the pixels you provide. It does **not** access your device,
  your Messages database, iCloud, or any message store. Nothing leaves your machine except what
  you send to the model when it reads the crops.
- **Not a forensic or legal-evidence tool.** The output is a best-effort reconstruction from
  screenshots or video, with confidence flags and possible gaps. Do not represent it as an
  authenticated or tamper-evident record of a conversation, and do not rely on it as evidence of
  what was said, by whom, or when.

## License

MIT
