# Output formats — canonical JSON, plain text, Markdown

The three deliverables. All render from one in-memory **canonical document**,
`{"metadata": {...}, "messages": [...]}`. `formats/json_out.py` is the source of truth for its
shape and validates it; `formats/text_out.py` and `formats/markdown_out.py` render from it and
assume (but don't require) a clean document.

## Canonical JSON schema

```json
{
  "metadata": {
    "skill": "unscroll",
    "platform": "string|null",
    "platform_confidence": "high|medium|low",
    "participants": 2,
    "date_range": {"start": "ISO8601|null", "end": "ISO8601|null"},
    "total_messages": 0,
    "sources_used": 0,
    "source_kind": "screenshots|video",
    "stitch_confidence": "high|medium|low",
    "flags": [],
    "generated_at": "ISO8601"
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

Metadata is built by `core.report.rollup`: `participants` = distinct senders (default 2);
`date_range` = min/max over non-null message timestamps; `stitch_confidence` = the weakest
adjacency confidence across the overlap matrix and the order stage; `sources_used` = frames in
the resolved order (also overwritten with the intake frame count by `run_pipeline`); `flags`
lists `fatal_gap:i->j`, `low_confidence_gap:i->j`, and any invariant-violation names. In the
`run` draft, every message's `content` is `null` and `confidence` is `low` until the model reads
the crops.

### Schema validation

`json_out.validate_doc(doc)` returns a list of problems (empty = valid). It checks all metadata
and message keys are present, every enum field is in range (`sender`, `type`, `confidence`,
`timestamp_source`), `content`/`timestamp` are string-or-null, and **`index` is strictly
monotonic increasing**. The `render` command runs this first and refuses to render a document
with problems, printing each one. `write_json` pretty-prints UTF-8 with `indent=2` and
`ensure_ascii=False` (so non-Latin text stays readable).

## Plain text (`text_out.write_text` → `transcript.txt`)

A flat, one-block-per-message rendering. A short header carries thread name, platform, date
range, and message/source counts. Each message is a header line plus indented body:

```
14:47 · Alex
  Hey, are you around?
```

- **Header line** is `HH:MM · NAME`; the time is omitted (name only) when the timestamp is
  missing or unparseable.
- **Sender label**: `self` → `You`, `unknown` → `Unknown`, otherwise the `display_name` or
  `Them`. The **thread name** is the first `other` sender's display name, else `Unknown Contact`.
- **Body**: text is printed as-is (split on newlines); a non-text type becomes a media
  placeholder; unread text (`content` null) becomes `[unreadable]`.

## Markdown dialogue script (`markdown_out.write_markdown` → `transcript.md`)

A paginated TV/film-style dialogue script — the human-readable deliverable.

### Structure

- **Page header** — a `════` rule block with thread, platform, date range, message/source
  counts, and `Page N of M`, repeated on every page.
- **Date section headers** — `── MONTH DAY, YEAR ──` (padded to the 52-char rule width) at each
  new calendar date.
- **Message block** — `H:MM AM/PM · NAME` (12-hour wall clock, name upper-cased) then indented
  body lines; name only when the timestamp is missing.

### Gap markers

Emitted between messages based on the silence between them:

- ≥ 4 hours → `── [N-HOUR GAP] ──`
- ≥ 24 hours → `── [N-DAY GAP] ──`
- ≥ 7 days → no explicit marker; the following date section header expresses the break on its own.

### Media placeholders

Non-text messages render as a bracketed placeholder on the body line (shared with the text
writer): `[IMAGE]`, `[AUDIO MESSAGE]`, `[VIDEO]`, `[STICKER]`, `[REACTION: …]`, `[LINK: …]`, and
`system` renders its content (or `[SYSTEM]`). Unknown types fall back to `[UPPERCASED-TYPE]`.

### Pagination

`_paginate` groups timestamped messages into pages. A page break is forced at a **calendar-month
boundary** or a **hard cap of 200 messages**, and preferred at a natural gap (≥ 4h) once a **soft
cap of 150** is reached — so dense conversations still paginate while sparse ones aren't
fragmented mid-exchange. Messages with a `null` timestamp never disturb dated pagination: they
are collected into a trailing `── UNDATED ──` section on a final page, so the writer never
crashes on missing time data.
