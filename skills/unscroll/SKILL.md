---
name: unscroll
description: >
  Reconstructs complete text message conversation histories from multiple partial screenshots
  into a searchable, portable archive. Use this skill whenever a user provides screenshots
  of a messaging conversation — uploaded as individual image files or a ZIP — and wants them
  assembled into a transcript, archive, or structured record. Triggers on: "stitch my
  screenshots", "combine these messages", "reconstruct my conversation", "make an archive
  of my texts", "export my chat history from screenshots", or any request to assemble
  fragmented views of a messaging thread into a readable whole. Also triggers when a user
  provides a batch of conversation screenshots without explicit instructions, clearly intending
  to do something with them together. Handles iOS Messages, WhatsApp (iOS and desktop),
  Android Messages, and other common platforms via extensible platform detection.
---

# Unscroll

Converts a batch of overlapping messaging app screenshots into a complete, portable
conversation record. Input is a set of partial screenshots of the same thread. Output
is a structured transcript in multiple formats, validated against a stitched visual image
of the full conversation.

The stitched image is a **validation artifact** — its primary purpose is to confirm that
the user's screenshot capture was complete and gapless. The deliverable outputs are the
transcript files.

---

## Inputs

**Accepted formats:**
- Individual image files (PNG, JPG, HEIC, WEBP), uploaded in any quantity
- A single ZIP archive containing image files of the same conversation

**Ordering:**
- Screenshots may be provided in any order. Do not ask the user to sort them.
- Use filesystem creation timestamp metadata and anchor-point detection (Phase 2) to
  establish order. File names are ignored entirely — treat them as opaque identifiers.
- If creation timestamps are all identical or absent, rely solely on anchor detection.

**Fatal precondition:**
Every adjacent pair of screenshots in the reconstructed sequence must have a
detectable visual overlap anchor. If any single pair lacks a valid anchor, **reject
the entire batch**. Do not produce partial output. See Phase 2 for anchor detection.

Report the rejection clearly:
> "Reconstruction failed. No overlap was found between [screenshot A] and [screenshot B].
> These appear to be non-adjacent screenshots. Please capture the missing portion of the
> conversation — you need at least one message visible in both screenshots where they meet."

---

## Outputs (Default)

All three are produced by default unless the user specifies otherwise.

| Output | Format | Description |
|--------|--------|-------------|
| **Transcript (script format)** | Markdown `.md` | Human-readable, paginated, dialogue-script style |
| **Structured export** | JSON `.json` | Machine-readable, message-level data with metadata |
| **Validation image** | PNG | Full stitched thread, used to verify capture completeness |

The validation image is not the primary deliverable and should be described to the user
as a completeness check. If the conversation is very long, note that the image may be
extremely tall and suggest viewing it in an image viewer that supports vertical scrolling.

---

## Processing Pipeline

### Phase 1 — Intake, Platform Detection, and Ordering

1. Accept all input files. If a ZIP was provided, extract contents to a working set.
   Discard non-image files silently.

2. For each screenshot, determine:
   - **Platform**: Load `references/platform-registry.md` and identify the platform
     using the visual fingerprinting checklist. This must be done before Phase 2
     because chrome boundary values depend on platform.
   - **Filesystem timestamp**: Read file creation or modification timestamp as a
     coarse ordering signal. Note if absent or identical across files.
   - **Edge content**: Note the first and last visible message in each screenshot
     (used during ordering validation in Phase 2).

3. Flag and report to the user (but do not halt):
   - Screenshots that appear to be from a different conversation or platform
   - Screenshots where platform could not be determined with confidence

4. Produce a provisional ordering based on timestamps. Phase 2 will validate and
   correct this ordering using anchor detection.

---

### Phase 2 — Anchor Detection and Order Validation

This is the critical phase. A failure here is a fatal error.

For each adjacent pair in the provisional order (screenshot[i], screenshot[i+1]):

#### Step 2a: Extract anchor strips

Strip top and bottom chrome from both screenshots using the platform chrome boundaries
from `references/platform-registry.md`. Work only within the content area.

From screenshot[i]: crop a strip from the **bottom** of the content area.
From screenshot[i+1]: crop a strip from the **top** of the content area.

Strip height: `max(3 complete message bubbles, 20% of content area height)`.

These are the **anchor strips** — horizontal slices capturing the zone that should
appear in both screenshots at their meeting point.

#### Step 2b: Match anchors

Slide the anchor strips against each other to find the row offset at which they
align. The alignment offset `d*` is the number of rows of visual overlap between
the two screenshots.

If no alignment is found above the minimum confidence threshold: **fatal anchor failure**.
Reject the batch. See the input section for the rejection message.

If the provisional order produces anchor failures but a reordering resolves them
(e.g., two screenshots were swapped), apply the reorder and note it in the output.
Only reject if no valid ordering of the provided screenshots produces a complete
unbroken chain.

See `references/overlap-detection.md` for the full matching algorithm, tolerance
values, and confidence scoring.

#### Step 2c: Select splice rows

Within each validated overlap zone, find the optimal splice row — a horizontal gap
between message bubbles, nearest the midpoint of the overlap zone.

Mark each splice row with its confidence level (high / medium / low).

---

### Phase 3 — Stitching (Validation Artifact)

1. For each screenshot, determine its content segment:
   - **First**: top of content area to its splice row with screenshot[1]
   - **Middle**: from splice row with screenshot[i-1] to splice row with screenshot[i+1]
   - **Last**: from splice row with screenshot[n-1] to bottom of content area

2. Strip all chrome from each segment. Retain only the message content region.

3. Concatenate segments vertically in order. This is the validation image.

4. Mark each splice point with a 1px hairline in a neutral, semi-transparent color.
   Offer a clean version without markers if the user requests it.

5. Save as PNG. Communicate to the user that this image's purpose is to let them
   visually confirm that the full conversation is present and properly assembled.

---

### Phase 4 — Structured Extraction

Extract message records from the source screenshots (higher per-message resolution
than the stitched image). Process screenshots in order; deduplicate messages that
appear in the overlap zones of adjacent screenshots.

For each message, record:

```json
{
  "index": 0,
  "sender": "self | other | unknown",
  "display_name": "string or null",
  "timestamp": "ISO 8601 string or null",
  "timestamp_source": "explicit | interpolated | absent",
  "content": "string or null",
  "type": "text | image | audio | video | sticker | reaction | link_preview | system",
  "confidence": "high | medium | low",
  "notes": "optional flag"
}
```

Rules:
- **Sender**: determined by bubble alignment (platform-specific; see platform registry)
- **Timestamps**: use visible in-thread labels as anchors; interpolate linearly between
  them; mark interpolated entries with `"timestamp_source": "interpolated"`
- **Unreadable content**: set `content` to null, `confidence` to `low`, note the reason.
  Do not guess at obscured or low-resolution text.
- **Deduplication**: messages visible in the overlap zone of two adjacent screenshots
  appear once in the output, not twice.

Top-level metadata block:

```json
{
  "metadata": {
    "skill": "unscroll",
    "platform": "string",
    "platform_confidence": "high | medium | low",
    "participants": 2,
    "date_range": { "start": "ISO 8601 or null", "end": "ISO 8601 or null" },
    "total_messages": 0,
    "screenshots_used": 0,
    "stitch_confidence": "high | medium | low",
    "flagged_segments": [],
    "generated_at": "ISO 8601"
  },
  "messages": []
}
```

---

### Phase 5 — Transcript (Script Format)

Generate the human-readable transcript from the Phase 4 JSON output.

#### Format

Use a modified TV/film dialogue script format:

```
════════════════════════════════════════════════════
UNSCROLL TRANSCRIPT
Thread: [contact name or "Unknown Contact"]
Platform: [platform]
Date range: [start] – [end]
Messages: [total]   Screenshots: [count]
Page [N] of [total pages]
════════════════════════════════════════════════════

── JUNE 14, 2020 ────────────────────────────────

2:47 PM · ALEX
  Hey, are you around?

2:47 PM · JAMIE
  Yeah what's up

2:48 PM · ALEX
  Can we talk later?

  [IMAGE]

2:49 PM · JAMIE
  Sure call me whenever

── [3-DAY GAP] ──────────────────────────────────

── JUNE 17, 2020 ────────────────────────────────

9:03 AM · ALEX
  Hey sorry about that
```

#### Pagination

Pages are bounded by **both** a time-window threshold and a message-count cap:
- New page at each calendar month boundary (default) OR after 150 messages,
  whichever comes first
- Always break pages at a natural gap between messages, not mid-exchange
- Page header on every page (thread info + page number)

For very active conversations, the message-count cap prevents single pages from
becoming unreadably long. For sparse conversations, monthly grouping may produce
many short pages — note this and offer to consolidate if the user prefers.

#### Gap markers

Insert a gap marker between messages where the silence exceeds:
- 4 hours: `── [N-hour gap] ──`
- 24 hours: `── [N-day gap] ──`
- 7+ days: new date section header `── [DATE] ──`

#### Media placeholders

For non-text messages, insert a bracketed descriptor on its own line:
`[IMAGE]`, `[AUDIO MESSAGE]`, `[VIDEO]`, `[STICKER]`, `[LINK: url-if-readable]`

Do not describe image contents unless the user explicitly requests it.

---

## Large Conversation Handling

For conversations exceeding approximately 1,000 messages or 50 screenshots:

- Note the scale upfront before processing begins
- The validation image will be extremely tall; confirm the user wants it generated
- The transcript will span many pages; confirm pagination preferences before generating
- Phase 6 analytics becomes more meaningful at this scale; offer it proactively

---

## Platform Detection

Load `references/platform-registry.md` at the start of Phase 1. Use the fingerprinting
checklist to identify the platform before applying any chrome boundary values or
sender-alignment logic.

Do not assume any specific platform. All platform-specific values must come from the
registry, not hardcoded assumptions in this skill body.

If platform cannot be determined with medium or higher confidence, ask the user
which app the screenshots are from before proceeding.

---

## Failure Modes

| Condition | Action |
|-----------|--------|
| Missing anchor between any adjacent pair | **Fatal rejection** — halt, report pair, request missing screenshot |
| No valid ordering resolves the chain | Fatal rejection |
| Platform unidentifiable | Ask user before proceeding |
| Screenshots from different conversations | Report and ask user to remove non-matching files |
| Resolution mismatch > 15% width | Warn; ask user to confirm before proceeding |
| Dark/light mode mix within batch | Warn; proceed; note in output |
| Group thread (3+ senders) | Proceed with reduced sender-ID confidence; note in output |
| Unreadable content | null content, low confidence flag — do not guess |
| Single screenshot | Skip Phases 2–3; proceed to extraction if requested |

---

## User Communication

**Before processing:**
- Confirm file count and types received
- Report detected platform(s) and confidence
- Confirm output mode preferences if ambiguous

**After processing:**
- Total messages reconstructed
- Confidence summary per splice point
- Any flags (gaps, low-confidence points, unreadable messages)
- Output file paths

Do not narrate processing steps inline. Surface warnings in the final report only.

---

## Environment Guidance

| Environment | Recommended approach |
|-------------|----------------------|
| Vision + code execution | Vision for Phases 1 & 4; pixel matching for Phases 2–3 |
| Vision only | Visual inspection throughout; note reduced splice precision in output |
| Code execution only | OCR for ordering + pixel matching throughout |

For pixel matching implementation, see `references/overlap-detection.md`.

---

## Reference Files

| File | Load when |
|------|-----------|
| `references/platform-registry.md` | Phase 1 — always |
| `references/overlap-detection.md` | Phase 2 — always |
| `references/analytics.md` | Phase 6 — on user request only |

---

## Version Notes

**v1 scope**: Two-party threads. ZIP and batch file input. Three default outputs.
iOS Messages, WhatsApp (iOS/desktop), Android Messages via platform registry.

**Out of v1 scope**: Archive management UI, bulk folder ingestion, group thread
sender disambiguation beyond "unknown sender N".
