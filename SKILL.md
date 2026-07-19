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
  to do something with them together. Platform-agnostic: assumes all screenshots in a batch
  come from the same device, operating system, and messaging app.
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

**Single-device assumption:**
All images in a batch are assumed to come from the same device, operating system,
and messaging app. There is no platform detection; chrome dimensions are measured
from the first image and applied to the whole batch (Phase 2).

**Ordering:**
- Screenshots may be provided in any order. Do not ask the user to sort them.
- Use filesystem creation timestamp metadata to establish a provisional order, oldest
  first; splice-point matching (Phase 3) validates and corrects it. File names are
  ignored entirely — treat them as opaque identifiers.
- If creation timestamps are absent or all identical, ask the user to confirm order
  before continuing.

**Fatal precondition:**
Every adjacent pair of screenshots in the reconstructed sequence must have a
detectable visual overlap. If any single pair lacks a valid overlap, **reject
the entire batch**. Do not produce partial output. See Phase 3 for splice matching.

Report the rejection clearly:
> "Reconstruction failed. No overlap was found between [screenshot A] and [screenshot B].
> These appear to be non-adjacent screenshots. Please capture the missing portion of the
> conversation — you need at least one message visible in both screenshots where they meet."

---

## Outputs (Default)

All three are produced by default unless the user specifies otherwise.

| Output | Format | Description |
|--------|--------|-------------|
| **Transcript (plain format)** | Markdown `.md` | Human-readable, minimalist dialogue format |
| **Structured export** | JSON `.json` | Machine-readable, message-level data with metadata |
| **Validation image** | PNG | Full stitched thread, used to verify capture completeness |

The validation image is not the primary deliverable and should be described to the user
as a completeness check. If the conversation is very long, note that the image may be
extremely tall and suggest viewing it in an image viewer that supports vertical scrolling.

---

## Processing Pipeline

### Phase 1 — Intake and Ordering

1. Accept all input files. If a ZIP was provided, extract contents to a working set.
   Discard non-image files silently.

2. For each screenshot, determine:
   - **Filesystem timestamp**: Read file creation or modification timestamp as a
     coarse ordering signal. Note if absent or identical across files.
   - **Edge content**: Note the first and last visible message in each screenshot
     (used during ordering validation in Phase 3).

3. Flag and report to the user (but do not halt):
   - Screenshots that appear to be from a different conversation

4. Produce a provisional ordering based on timestamps, oldest first. If timestamps
   are absent or all identical, ask the user to confirm order before continuing.
   Phase 3 will validate and correct this ordering using splice matching.

---

### Phase 2 — Chrome Detection and Stripping

Using the **first image only**, detect the chrome boundaries:

- **Top boundary**: scan downward from row 0. The top chrome ends at the first row
  containing message content — defined as a horizontal run of non-background pixels
  of at least 40px within the center 60% of image width.
- **Bottom boundary**: scan upward from the last row. The bottom chrome ends at the
  first such row from the bottom.

Record these as `chrome_top_px` and `chrome_bottom_px`.

Apply these exact pixel values to crop **all** images in the batch. The first image
defines the chrome dimensions for the entire batch — do not re-detect for subsequent
images. Store all cropped images in memory as the working set.

If the detected boundaries seem implausible (content area less than 20% of image
height, or boundaries not found), report the detected values to the user and ask
to confirm before continuing.

See `references/overlap-detection.md` for the content-row definition and background
color estimation.

---

### Phase 3 — Sequential Stitching

This is the critical phase. A failure here is a fatal error.

For each adjacent pair of cropped images (image[i], image[i+1]):

#### Step 1 — Find splice point in image[i]

Scan upward from the bottom edge of the cropped image, one row at a time. The splice
point is the bottommost horizontal slice of 1–3px that contains message content
(non-background pixels). This is the last row of visible content in image[i].

If no content row is found in image[i], this is a fatal error. Report which image
failed and halt.

#### Step 2 — Locate splice point in image[i+1]

Starting from the top of image[i+1]'s cropped content, scan downward row by row.
Find the first row whose pixel content matches the splice point slice from image[i],
above the confidence threshold defined in `references/overlap-detection.md`.

If no matching row is found, this is a fatal error. Report which pair failed, specify
that coverage is missing between these two screenshots, and halt. Do not stitch.

If the provisional order produces match failures but a reordering resolves them
(e.g., two screenshots were swapped), apply the reorder and note it in the output.
Only reject if no valid ordering of the provided screenshots produces a complete
unbroken chain.

Record each splice point's match confidence (high / medium; low is a fatal error —
see `references/overlap-detection.md` for scoring).

#### Step 3 — Crop and splice

Remove everything above the matching row from image[i+1]. Concatenate image[i] with
the trimmed image[i+1], later image below. Repeat for each subsequent pair, always
building onto the growing composite.

Mark each splice point with a 1px hairline in a neutral, semi-transparent color.
Offer a clean version without markers if the user requests it.

#### Step 4 — Export

Save the final composite as a lossless PNG. Communicate to the user that this
image's purpose is to let them visually confirm that the full conversation is
present and properly assembled.

---

### Phase 4 — Structured Extraction

Extract message records from the source screenshots (higher per-message resolution
than the stitched composite). Process screenshots in order; deduplicate messages that
appear in the overlap zones of adjacent screenshots.

Assign a sequential integer index, starting at 1, to each media item encountered
(images, audio, video, stickers). These will be referenced in transcripts as
`[inline image #N]`, `[inline audio #N]`, etc.

For each message, record:

```json
{
  "index": 0,
  "sender": "self | other | unknown",
  "display_name": "string or null",
  "timestamp": "string exactly as visible in image, or null",
  "timestamp_type": "cluster | per_message | null",
  "content": "string or null",
  "type": "text | image | audio | video | sticker | reaction | link_preview | system",
  "media_index": "integer or null",
  "confidence": "high | medium | low",
  "notes": "optional flag"
}
```

Rules:
- **Sender**: right-aligned bubble = `self`; left-aligned = `other`; use `unknown`
  when alignment is ambiguous or sender cannot be determined
- **Timestamp**: copy exactly as it appears in the image — format, wording, and all.
  Do not reformat, normalize, or convert to ISO 8601 in this field. If no timestamp
  is visible for a message, set to null. Do not interpolate.
- **Unreadable content**: set `content` to null, `confidence` to `low`, note the reason.
  Do not guess at obscured or low-resolution text.
- **Deduplication**: messages visible in the overlap zone of two adjacent screenshots
  appear once in the output, not twice.
- **Media items**: each gets a sequential `media_index`. Record full details in the
  top-level `media` array.

Top-level structure:

```json
{
  "metadata": {
    "skill": "unscroll",
    "participants": 2,
    "date_range": { "start": "ISO 8601 or null", "end": "ISO 8601 or null" },
    "total_messages": 0,
    "total_media": 0,
    "screenshots_used": 0,
    "stitch_confidence": "high | medium | low",
    "flagged_segments": [],
    "generated_at": "ISO 8601"
  },
  "media": [
    {
      "index": 1,
      "type": "image | audio | video | sticker",
      "description": "brief factual description",
      "is_conversation_screenshot": false,
      "transcription": null
    }
  ],
  "messages": []
}
```

Metadata timestamps (`date_range`, `generated_at`) are metadata, not transcript data;
normalizing them to ISO 8601 is acceptable there and only there.

For media items that are themselves screenshots of a text conversation:
set `is_conversation_screenshot: true` and populate `transcription` with as much
of the visible conversation text as can be read. Do not describe the screenshot —
transcribe it.

---

### Phase 5 — Plain Transcript

Generate from the Phase 4 JSON output.

#### Format rules

- No headers, section dividers, ASCII decorations, or document marks of any kind
- No added commentary, annotations, asides, or explanatory text
- No gap markers or silence indicators
- No day sections or date groupings added by the tool
- Speaker names in uppercase, followed by two spaces, followed by message text on the same line
- Timestamps appear only where they were visible in the source images, on their own line,
  copied verbatim. Cluster timestamps that appeared between message groups in the source
  images are source data and are included as-is. Timestamps are not added, reformatted,
  or interpolated by the tool.
- Media items appear as inline placeholders at the position they occurred: no description inline

Example output:

```
Today 2:47 PM

ALEX  Hey, are you around?

JAMIE  Yeah what's up

ALEX  Can we talk later?

[inline image #1]

JAMIE  Sure call me whenever

Monday 9:03 AM

ALEX  Hey sorry about that
```

#### Media appendix

After the transcript body, include a media appendix with no decorative formatting.
Header is the single word `Media` on its own line. Each item is its index number,
type, and a brief description on one line.

If the item is itself a screenshot of a text conversation, transcribe the visible
conversation content instead of describing it. Use the same format as the main
transcript (uppercase speaker names, verbatim text), indented with two spaces.

```
Media

1  image — photo of a receipt
2  audio message
3  image — screenshot of a text conversation
     PERSON A  visible text here
     PERSON B  visible reply here
4  video
```

---

### Phase 6 — Analytics (optional)

Load `references/analytics.md` before running this phase. Confirm with the user first.
Requires Phase 4 output as input.

---

## Large Conversation Handling

For conversations exceeding approximately 1,000 messages or 50 screenshots:

- Note the scale upfront before processing begins
- The validation image will be extremely tall; confirm the user wants it generated
- The transcript will be very long; note this in the final report
- Phase 6 analytics becomes more meaningful at this scale; offer it proactively

---

## Failure Modes

| Condition | Action |
|-----------|--------|
| No content row found in image[i] | **Fatal** — report image, request replacement |
| No matching row found for a pair | **Fatal** — report pair, specify coverage is missing, request missing screenshot |
| No valid ordering resolves the chain | Fatal rejection |
| Chrome boundaries implausible | Report detected values; ask user to confirm |
| Timestamps absent or all identical | Ask user to confirm order before proceeding |
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
- Confirm ordering basis (timestamps, or user-confirmed order)
- Confirm output mode preferences if ambiguous

**After processing:**
- Total messages reconstructed
- Confidence summary per splice point
- Any flags (low-confidence points, unreadable messages)
- Output file paths

Do not narrate processing steps inline. Surface warnings in the final report only.

---

## Environment Guidance

| Environment | Recommended approach |
|-------------|----------------------|
| Vision + code execution | Code for Phases 2–3; vision for Phase 4 |
| Vision only | Visual inspection throughout; note reduced splice precision in output |
| Code execution only | Pixel matching for Phases 2–3; OCR for Phase 4 |

For pixel matching implementation, see `references/overlap-detection.md`.

---

## Reference Files

| File | Load when |
|------|-----------|
| `references/overlap-detection.md` | Phases 2–3 — always |
| `references/analytics.md` | Phase 6 — on user request only |

---

## Version Notes

**v1 scope**: Two-party threads. ZIP and batch file input. Three default outputs.
Single-device assumption; platform- and app-agnostic (no platform detection).

**Out of v1 scope**: Archive management UI, bulk folder ingestion, group thread
sender disambiguation beyond "unknown sender N".
