# unscroll

Turn overlapping chat screenshots — or a scroll-capture video — into a structured transcript
(JSON, plain text, Markdown). unscroll is a [Claude Code](https://claude.com/claude-code)
plugin: deterministic Python scripts that do all the geometry, plus a skill that tells the
model how to drive them.

Built for iOS-Messages-style and generic two-party chats. There is no per-app or per-device
configuration — on-screen chrome (status bar, nav bar, input bar, keyboard) is detected at
runtime.

## What it does

Given a pile of partial screenshots in any order, or a screen recording of you scrolling:

1. Detects and masks the UI chrome on every frame.
2. Reconstructs the true top-to-bottom order from image content, not filenames or timestamps.
3. Stitches a tall `validation.png` so you can confirm the capture is complete and gapless.
4. Segments each message, de-duplicates the copies that recur in overlaps, and writes one
   high-res crop per message.
5. Hands those crops to the model to read, then renders the filled transcript.

**unscroll does no text recognition of its own.** The scripts are pure geometry — variance,
correlation, histograms. Every word of text comes from a multimodal model reading the crops,
which is a required step, not an optimization. The stitched image is a completeness check;
the transcript files are the deliverable.

## Install

```
/plugin marketplace add jagp/unscroll
/plugin install unscroll
```

For local development: `claude --plugin-dir /path/to/unscroll`

## Usage

Just give Claude your screenshots or scroll video and ask it to reconstruct the conversation.
Under the hood it runs two commands:

```bash
# 1. Geometry pass — input is a folder, .zip, single image, or video
python skills/unscroll/scripts/unscroll.py run <input> --workdir <workdir>

# 2. After the model reads <workdir>/crops/*.png and fills in the text:
python skills/unscroll/scripts/unscroll.py render <filled.json> --workdir <workdir>
```

`run` also takes `--platform` (a metadata label only), `--no-mark` (omit seam hairlines in
the validation image), and `--no-timestamps`. It exits `2` if a fatal gap is found.
`render` takes `--formats` (default `json,text,markdown`) and exits `1` on a malformed
document. These are the only two subcommands.

Artifacts land in `<workdir>`:

| File | What it is |
|---|---|
| `report.json` | Order, confidence, flags, fatal gaps |
| `validation.png` | The stitched thread — a completeness check |
| `crops/msg_*.png` | One crop per message, in order |
| `transcript.json` / `.txt` / `.md` | The transcript (`content` is `null` until the model fills it) |

## Requirements

- Python 3.14
- `numpy>=1.24`, `Pillow>=10.0` — the only Python dependencies
- `ffmpeg` — needed only for video input and HEIC decode

No OpenCV, torch, or scipy.

## Limitations

- **Overlap is required.** Adjacent captures must share at least one message. If a stretch was
  never captured, unscroll names the frame pair and stops rather than fabricating the gap.
- **Two-party threads** are the target; group threads work with reduced sender confidence.
- **Sender is inferred from bubble side** (left/right) and can be `unknown` for centered or
  full-width content. There is no name detection.
- **Message type is guessed as text or image only.** Audio, stickers, reactions, and link
  previews are only ever labelled by the model, never by the geometry.
- **Metadata is best-effort.** Timestamps and media types are filled only when legibly
  visible; otherwise left null and flagged. Blurry text is left unread, not guessed.
- **Thresholds are tuned against synthetic fixtures** and still want real-device calibration.
  Drop real captures into `tests/fixtures/` to calibrate on real pixels.

## Privacy and scope

**Image-only.** unscroll reads only the pixels you give it. It does not access your device,
your Messages database, or iCloud. Nothing leaves your machine except the crops you send to
the model.

**Not a forensic or legal-evidence tool.** The output is a best-effort reconstruction with
confidence flags and possible gaps. Do not represent it as an authenticated or tamper-evident
record, or rely on it as evidence of what was said, by whom, or when.

## License

MIT
