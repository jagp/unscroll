"""core/report.py — confidence rollup, invariant self-tests, document assembly.

Three responsibilities:

* :func:`rollup` builds the canonical ``metadata`` block (see CONTRACT.md
  "Canonical JSON schema") from the pipeline's stage results.  It never crashes
  on empty / partial inputs — missing signals degrade to ``low`` confidence and
  ``null`` fields rather than raising.
* :func:`invariants` runs a few cheap self-tests over the stitched output and
  returns the names of any *violated* invariants.  Callers downgrade confidence
  (or halt) rather than trusting output that fails them.
* :func:`assemble_document` glues ``metadata`` + ``messages`` into the canonical
  document the ``formats/`` writers consume.

Pure stdlib.  No third-party dependencies.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["rollup", "invariants", "assemble_document"]

# Confidence ordering — higher rank is stronger.  "none" collapses to "low" for
# the output enum (which only admits high|medium|low).
_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
_RANK_TO_NAME = {0: "low", 1: "low", 2: "medium", 3: "high"}

# Tolerance (px) for the stitched-height invariant: a couple of pixels of slop
# plus one per seam, to absorb integer rounding and 1px splice hairlines.
_STITCH_TOL_BASE = 2


# ---------------------------------------------------------------------------
# small accessors — tolerate both dicts and attribute-bearing objects
# ---------------------------------------------------------------------------
def _get(obj, name, default=None):
    """Fetch ``name`` from ``obj`` whether it is a dict or an object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _confidence_of(obj) -> str | None:
    """Return the ``confidence`` string of an OverlapResult-like object."""
    conf = _get(obj, "confidence")
    return conf if isinstance(conf, str) and conf in _RANK else None


def _iter_overlaps(overlaps):
    """Yield OverlapResult-like items from a list, tuple, or dict-of-values."""
    if overlaps is None:
        return
    if isinstance(overlaps, dict):
        yield from overlaps.values()
    elif isinstance(overlaps, (list, tuple)):
        yield from overlaps


# ---------------------------------------------------------------------------
# rollup
# ---------------------------------------------------------------------------
def rollup(
    order_result,
    overlaps,
    splices,
    messages,
    source_kind: str = "screenshots",
    platform_hint: str | None = None,
    now=None,
) -> dict:
    """Build the canonical ``metadata`` block; never crashes on empty inputs."""
    messages = messages or []

    # participants: distinct senders present, defaulting to 2 when unknowable.
    senders = {
        _get(m, "sender")
        for m in messages
        if _get(m, "sender") not in (None, "")
    }
    participants = len(senders) if senders else 2

    # date_range: min/max over non-null message timestamps (ISO8601 sorts lexically).
    timestamps = sorted(
        ts for ts in (_get(m, "timestamp") for m in messages) if ts
    )
    date_range = {
        "start": timestamps[0] if timestamps else None,
        "end": timestamps[-1] if timestamps else None,
    }

    # stitch_confidence: the weakest adjacency confidence across overlaps and the
    # order stage's own confidence.  Absent any signal -> "low".
    ranks = [
        _RANK[c]
        for c in (_confidence_of(o) for o in _iter_overlaps(overlaps))
        if c is not None
    ]
    order_conf = _confidence_of(order_result)
    if order_conf is not None:
        ranks.append(_RANK[order_conf])
    stitch_confidence = _RANK_TO_NAME[min(ranks)] if ranks else "low"

    # sources_used: number of frames in the resolved order, if reported.
    order_list = _get(order_result, "order")
    sources_used = len(order_list) if isinstance(order_list, (list, tuple)) else 0

    # flags: fatal / low-confidence adjacency gaps, plus any explicit flags the
    # order stage recorded.  Invariant violations are surfaced by the caller
    # (which runs invariants() and extends this list) — see module note.
    flags: list[str] = []
    for gap in _get(order_result, "gaps", []) or []:
        between = _get(gap, "between")
        if isinstance(between, (list, tuple)) and len(between) == 2:
            i, j = between
        else:
            i = _get(gap, "i", _get(gap, "from"))
            j = _get(gap, "j", _get(gap, "to"))
        pair = f"{i}->{j}" if i is not None or j is not None else "?"
        if _get(gap, "fatal"):
            flags.append(f"fatal_gap:{pair}")
        elif _confidence_of(gap) == "low" or _get(gap, "low_confidence"):
            flags.append(f"low_confidence_gap:{pair}")
    for extra in _get(order_result, "flags", []) or []:
        if extra not in flags:
            flags.append(extra)

    platform_confidence = "high" if platform_hint else "low"

    metadata = {
        "skill": "unscroll",
        "platform": platform_hint,
        "platform_confidence": platform_confidence,
        "participants": participants,
        "date_range": date_range,
        "total_messages": len(messages),
        "sources_used": sources_used,
        "source_kind": source_kind,
        "stitch_confidence": stitch_confidence,
        "flags": flags,
        "generated_at": _iso_now(now),
    }
    return metadata


def _iso_now(now) -> str:
    """Resolve the injectable ``now`` to an ISO8601 string (default: UTC now)."""
    if now is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(now, datetime):
        return now.isoformat()
    if callable(now):
        return _iso_now(now())
    return str(now)


# ---------------------------------------------------------------------------
# invariants
# ---------------------------------------------------------------------------
def invariants(stitched_height, segment_heights, messages) -> list[str]:
    """Cheap self-tests; return the names of VIOLATED invariants (empty = clean)."""
    violated: list[str] = []
    segment_heights = list(segment_heights or [])
    messages = messages or []

    # (a) stitched height should equal the sum of segment heights (± tolerance).
    seg_sum = sum(int(h) for h in segment_heights)
    tol = _STITCH_TOL_BASE + len(segment_heights)
    if stitched_height is None or abs(int(stitched_height) - seg_sum) > tol:
        violated.append("stitch_height_mismatch")

    # (b) message index must be strictly monotonic increasing.
    prev = None
    for m in messages:
        idx = _get(m, "index")
        if not isinstance(idx, int) or (prev is not None and idx <= prev):
            violated.append("nonmonotonic_index")
            break
        prev = idx

    # (c) no two ADJACENT messages share identical non-null content.
    for a, b in zip(messages, messages[1:]):
        ca, cb = _get(a, "content"), _get(b, "content")
        if ca is not None and ca == cb:
            violated.append("duplicate_adjacent_content")
            break

    return violated


# ---------------------------------------------------------------------------
# document assembly
# ---------------------------------------------------------------------------
def assemble_document(messages, metadata) -> dict:
    """Return the canonical ``{"metadata": ..., "messages": ...}`` document."""
    return {"metadata": metadata, "messages": list(messages or [])}
