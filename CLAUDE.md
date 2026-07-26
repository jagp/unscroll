# CLAUDE.md

Guidance for working on this repo. For what the tool is, see [README.md](README.md); for how
the model drives it, see [skills/unscroll/SKILL.md](skills/unscroll/SKILL.md).

## Layout

```
.claude-plugin/          plugin.json + marketplace.json
skills/unscroll/
  SKILL.md               the model-facing skill (the one Claude Code loads)
  references/*.md        detail docs, loaded on demand
  scripts/
    unscroll.py          orchestrator + CLI (run, render)
    core/*.py            geometry: imageio, chrome, video, offset, order,
                         splice, stitch, segment, report, checkpoint
    formats/*.py         json_out, text_out, markdown_out
    tests/               unittest suite + synthetic fixtures
    CONTRACT.md          module contracts (see drift note below)
docs/webapp-spec.md      draft spec for a future web app; not implemented
```

## Tests

```bash
cd skills/unscroll/scripts && python -m unittest discover -s tests
```

96 tests, stdlib `unittest` only — no pytest. They must be run from `scripts/` because the
suite imports `core.*` and `tests.*` as top-level packages. Fixtures are synthesized in
`tests/make_fixtures.py`, so the suite needs no real images and no network.

Real captures dropped into `tests/fixtures/` are for threshold calibration; nothing in the
suite depends on them.

## Constraints

- **numpy and Pillow only.** No OpenCV, scipy, or torch. `ffmpeg` is shelled out to, and only
  for video intake and HEIC decode.
- **No OCR anywhere.** The scripts are pure geometry. Text enters the transcript only when a
  multimodal model reads the crops. Don't add a text-recognition path without a deliberate
  decision — the whole design assumes that split.
- **Overlap semantics are contractual.** `vertical_offset(A, B) -> overlap` means the last
  `overlap` rows of A equal the first `overlap` rows of B, in original content-row
  coordinates. Splice `s` means A contributes `[0 : H_A - overlap + s]` and B contributes
  `[s : H_B]`. Getting this wrong silently duplicates or drops content.
- **`core/offset.py` gets the highest scrutiny.** Everything downstream is built on its
  overlap numbers.

## Known drift from CONTRACT.md

CONTRACT.md documents the intended design; a few parts were never built. Trust the code:

- It lists nine CLI subcommands (`intake`, `chrome`, `overlap`, …). **Only `run` and `render`
  exist.**
- It describes runs as resumable via the checkpoint manifest. `core/checkpoint.py` is fully
  implemented and `manifest.json` is written, but `Manifest.cached()` is never called — nothing
  is actually skipped on re-run.
- Several signatures have since gained parameters (`dedup_bands(..., crops_dir)`,
  `rollup(..., source_kind, now)`, `select_keyframes(..., return_details)`).
- `per_row_corr` has length `overlap // ds`, not `overlap`, unless the ds=1 refinement ran.
  `stitch._resample_corr` handles both; this one is flagged in the code.

Also unwired: `ContentSlice.confidence` is computed but never read, and `SelectionResult`
video diagnostics (blur/pause/bounce drops, coverage gaps) are computed but never surfaced in
`report.json`.

## Conventions

- Gitflow: `develop` is the integration branch and the default PR base. Feature branches are
  `feature/*`. Don't push to `main`.
- Tests are written mutation-first: before adding an assertion, name the production change
  that would break it, apply that mutant, watch the test fail, then revert. Assertions that no
  realistic mutation can break (`getsize() > 0`, membership in the set of all possible values)
  are treated as bugs.
