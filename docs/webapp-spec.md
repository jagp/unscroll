# Unscroll Web — Product & Technical Specification (Draft v0.1)

**Status:** draft for review · **Author:** generated 2026-07-05 · **Depends on:** the unscroll
engine (`skills/unscroll/scripts/`, 88 tests green)

---

## 1. What this is

A full-fledged web application that houses the entire unscroll process end to end. A user signs
in, uploads **overlapping screenshots** or a **scroll-capture video** of an async text-message
thread, watches the pipeline reconstruct it, reviews and corrects the result, and exports a
structured transcript (JSON / Markdown / plain text) plus the stitched validation image.

The existing Python engine is the core; the web app is the shell around it:

```
┌───────────────────────────── Unscroll Web ─────────────────────────────┐
│  Upload UI ──▶ Job pipeline (the existing engine) ──▶ Review UI ──▶ Export │
│                     │                                                   │
│                     └──▶ Claude API (vision) reads message crops        │
└─────────────────────────────────────────────────────────────────────────┘
```

Constraints inherited from the engine (unchanged):
- **Image/video only.** No device, iCloud, or message-database access — ever.
- **No per-app/device registry.** Chrome detection stays runtime/temporal.
- **Puntables.** Media, timestamps, reactions are best-effort; unreadable → null + low
  confidence; only an unbridgeable overlap gap is fatal.
- **Not a forensic/evidence tool** — same disclaimer as the plugin README.

---

## 2. Users and jobs-to-be-done

| Persona | Job |
|---|---|
| **The archivist** | "I have 80 screenshots of a conversation that matters to me. Turn them into something I can search, keep, and print." |
| **The migrator** | "I'm leaving this platform / lost this phone. This scroll video is all I have." |
| **The organizer** (v1.5+) | "I do this for several conversations; I need a library of past reconstructions." |

Primary emotional context: these are often **personal, sensitive conversations** (breakups,
disputes, memories of someone lost). The privacy posture (§9) is a product feature, not
compliance boilerplate.

---

## 3. v1 scope

**In:**
1. Account + auth (email magic-link to start; OAuth later).
2. Project ("thread") creation; multiple projects per user.
3. Upload: drag-drop multiple images, a ZIP, or one video. Resumable upload for video (files
   can be 100 MB+). Client-side validation of type/size before transfer.
4. Processing: the full engine pipeline as an async job with **live stage-by-stage progress**
   (intake → chrome → overlap → order → stitch → segment → read → render).
5. Fatal-gap UX: if the engine reports a gap, show *which two frames* don't connect, with their
   thumbnails side by side, and prompt: "capture the missing section and add it here." Added
   files trigger an **incremental re-run** (the engine's checkpoint manifest makes this cheap).
6. Read stage: Claude API vision fills message text/timestamps/senders from the per-message
   crops (§7).
7. Review UI: validation image beside an editable message list; user corrects text, senders,
   timestamps; corrections re-render exports instantly.
8. Export: `transcript.json` (canonical), `transcript.md`, `transcript.txt`, `validation.png`,
   or a ZIP of all. Download only — no public share links in v1.
9. Deletion: one-click project deletion that actually deletes (files, crops, DB rows, exports).

**Out (v1):** team/sharing features, public links, group-chat sender naming beyond
self/other/unknown, payment/billing (soft quotas only), mobile app (responsive web only),
OCR-only offline mode.

---

## 4. User journey (happy path)

1. **Create project** → name it, pick source kind (screenshots / video / "not sure").
2. **Upload** → thumbnails appear as files land; video shows duration + first-frame preview.
3. **Process** → progress card per stage; typical thread completes in tens of seconds of
   geometry + the read stage (dominated by Claude API latency).
4. **Gap check** → if fatal gaps: blocking screen with the failing pair and re-upload slot.
   Otherwise proceed automatically.
5. **Review** → split view: left = scrollable validation image with seam markers; right =
   message list. Clicking a message highlights its band in the image and vice versa. Confidence
   chips (high/med/low) filterable; "show unreadable only" toggle. Inline edit of text, sender
   side, timestamp.
6. **Export** → format checkboxes → download. Project persists (until deleted) for re-export.

---

## 5. Architecture

**Recommendation: a modular Python monolith + worker, not microservices.** The engine is
Python; the app should be too, so the pipeline is a library call, not an RPC hop.

```
┌────────────┐   HTTPS    ┌──────────────────────┐   enqueue   ┌────────────────────┐
│ React SPA  │ ─────────▶ │  FastAPI app server  │ ──────────▶ │  Worker (arq/RQ)    │
│ (Vite/TS)  │ ◀───SSE─── │  auth · REST · SSE   │ ◀──status── │  runs run_pipeline() │
└────────────┘            └───────┬──────────────┘             │  + Claude read stage │
                                  │                            └────────┬────────────┘
                          ┌───────▼───────┐                    ┌────────▼───────────┐
                          │ Postgres      │                    │ Object storage      │
                          │ (users, jobs, │                    │ (S3/R2: uploads,    │
                          │  messages)    │                    │  crops, exports)    │
                          └───────────────┘                    └────────────────────┘
                                                  Redis: queue + progress pub/sub
```

Key decisions:
- **Engine as a package.** Publish `skills/unscroll/scripts/` as an internal package
  (`unscroll-engine`) consumed by the worker. No code duplication; the plugin and the web app
  share one engine and one test suite.
- **Worker owns ffmpeg + numpy work.** The app server never does CPU-heavy work; every
  pipeline run is a queued job with a per-stage heartbeat written to Redis, streamed to the
  client over SSE.
- **Checkpointing maps to resumability.** The engine's content-hash manifest means a re-run
  after adding a gap-filling screenshot only recomputes affected pairs — surface this as
  "smart re-run" in the UI.
- **Deployment (v1):** one container image (server + worker entrypoints), Postgres, Redis,
  S3-compatible bucket. Runs on Fly.io/Railway/a VPS unchanged.
  *Alternative evaluated:* Cloudflare Workers front + Containers for the Python engine + R2/D1.
  Viable, and R2 is attractive for storage cost; deferred because the engine needs a real
  Python+ffmpeg runtime anyway, and one platform beats two while the product finds its shape.

---

## 6. Pipeline-as-jobs

One `Job` row per processing run, with stages mirroring the engine:

| Stage | Runs | Emits |
|---|---|---|
| `intake` | engine `intake()` (+ HEIC transcode, video keyframing) | frame count, source kind |
| `chrome` | `content_slices()` | per-frame content bounds |
| `overlap` | `build_overlap_matrix()` | pair scores |
| `order` | `order_frames()` (retry ladder inside) | order, gaps[], confidence |
| `stitch` | `compute_splices()` + `stitch()` | `validation.png` |
| `segment` | `segment_messages()` + `dedup_bands()` | crops/, partial transcript |
| `read` | **Claude API vision** over crops (§7) | filled messages |
| `render` | `render_document()` | exports |

Failure policy = the engine's: only `order`-stage fatal gaps block; everything else degrades
with flags. A crashed stage retries once, then marks the job `failed` with the stage + error
preserved for support. Jobs are idempotent per the checkpoint manifest.

---

## 7. The read stage (Claude API)

The one stage the plugin got "for free" (the model reading crops in-session) becomes explicit
API usage here.

- **Model:** `claude-opus-4-8` (vision-capable; $5/$25 per MTok). Revisit per-tier routing
  only with real cost data — don't pre-optimize.
- **Input:** batched requests, ~10–20 crops per request as base64 `image` blocks with an
  instruction to transcribe each message exactly (no paraphrase, no guessing obscured text —
  return null + reason instead), identify visible timestamps and display names, and correct
  the geometric `type` guess.
- **Output:** **structured outputs** (`output_config.format` with a JSON schema mirroring the
  canonical message record) so responses parse deterministically — no regex post-processing.
- **Batch API option:** for very large threads (500+ messages) offer "economy processing" via
  the Message Batches API — 50% cheaper, results usually well under an hour; the job's `read`
  stage polls the batch. Interactive default stays synchronous.
- **Privacy:** requests carry only the crop images + instructions; API calls are made
  server-side with the app's key. State plainly in the privacy page that message images are
  sent to Anthropic for transcription and are subject to Anthropic's API data handling.
- **Cost note (order of magnitude):** a message crop is small (~200–600 px tall); tens of
  messages fit in a few thousand image tokens. A 300-message thread is expected to cost cents
  to low tens of cents to read — meter it per project and show the user nothing (v1 absorbs
  cost under quota; billing is out of scope).

---

## 8. Data model (Postgres)

```
users        (id, email, created_at, …)
projects     (id, user_id, title, source_kind, status, created_at, deleted_at)
uploads      (id, project_id, object_key, kind[image|video|zip], bytes, sha256, created_at)
jobs         (id, project_id, status[queued|running|gap_blocked|failed|done],
              stage, progress, error, engine_report jsonb, created_at, finished_at)
frames       (id, project_id, idx, object_key, width, height, content_top, content_bottom)
messages     (id, project_id, idx, sender, display_name, timestamp, timestamp_source,
              content, type, confidence, notes, crop_key, edited_by_user bool)
exports      (id, project_id, format, object_key, created_at)
```

`messages` is the canonical document, exploded into rows so the review UI can PATCH a single
message; `render` re-assembles the canonical JSON from rows. `engine_report` stores the raw
`report.json` for debugging/support.

Object storage layout: `u/{user}/p/{project}/uploads/…`, `…/frames/…`, `…/crops/…`,
`…/exports/…` — a project delete is one prefix delete + row cascade.

---

## 9. Privacy & security (product-level feature)

- **Encryption:** TLS in transit; SSE/at-rest encryption on the bucket and DB volume.
- **Retention:** uploads + intermediates auto-delete N days after last activity (default 30,
  user-configurable down to "delete on export"). Exports persist until project deletion.
- **True deletion:** delete = purge storage prefix + hard-delete rows within 24h; no soft-keep.
- **No training / no resale:** message content is never used for anything but the user's own
  reconstruction. Anthropic API usage disclosed (§7).
- **Access:** projects strictly user-scoped; signed, short-lived URLs for all media; no
  guessable object keys.
- **Abuse posture:** the tool reconstructs *the user's own captures*; ToS prohibits processing
  conversations obtained unlawfully. No public sharing in v1 keeps the surface small.
- **Not evidence:** the README's forensic disclaimer appears in-product on every export.

---

## 10. Non-functional targets (v1)

| Concern | Target |
|---|---|
| Upload | ≤ 500 MB video, ≤ 200 images or one ZIP per project |
| Geometry stages | < 60 s for 50 screenshots on one worker core |
| Read stage | < 2 min interactive for ≤ 300 messages; Batch path for larger |
| Concurrency | queue-based; N workers horizontal-scale; per-user 2 concurrent jobs |
| Quotas (pre-billing) | e.g. 5 projects / 2,000 read-messages per user per month |
| Observability | per-stage timing + engine flags logged; job failure alerting |

---

## 11. Milestones

- **M0 — Spec sign-off** (this document).
- **M1 — Engine service skeleton:** `unscroll-engine` package extraction; FastAPI + worker +
  Postgres + storage; upload → geometry pipeline → validation.png visible in a bare UI. No
  auth beyond a dev login. *Proves the port.*
- **M2 — Read + review:** Claude read stage with structured outputs; review/edit UI; export
  of all three formats. *Proves the product.*
- **M3 — Accounts + privacy:** real auth, quotas, retention/deletion, privacy page, gap-blocked
  re-upload flow polish. *Makes it shippable.*
- **M4 — Hardening:** Batch-API economy path, incremental re-runs surfaced in UI, load tests,
  error-budget alerting, beta invite.

---

## 12. Open decisions (need your call)

1. **Hosting bias** — simple PaaS monolith (recommended for M1–M3) vs. committing to the
   Cloudflare stack early (Workers + Containers + R2)?
2. **Auth provider** — roll magic-link in-house vs. Clerk/Auth0/Supabase Auth?
3. **Read-stage default** — synchronous interactive reads for everyone, or default large
   threads (>N messages) into the Batch path with an email-when-done?
4. **Retention default** — 30 days vs. more aggressive "delete intermediates on export"?
5. **Name/domain** — is the product "Unscroll" outward-facing?
