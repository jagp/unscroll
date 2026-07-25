"""core/order.py — reconstruct the true top-to-bottom screenshot sequence.

Screenshots arrive in arbitrary order (file timestamps are, at best, a weak
prior). The real ordering signal is *visual overlap*: the bottom of screenshot
``i`` re-appears at the top of its true successor ``j``. :func:`core.offset.vertical_offset`
scores that directed relationship. This module turns those pairwise scores into a
single ordered chain.

Pipeline
--------
0. :func:`order_frames_chain_first` — the preferred entry point. Same-device
   captures almost always arrive already ordered (screenshot filenames, video
   keyframes), so it tests only the ``n-1`` consecutive pairs first and accepts
   the input order when every adjacency validates. A reversed capture order is
   probed at the same O(n) cost. Only when the capture order cannot be confirmed
   does it fall back to the full matrix + :func:`order_frames` below — seeded
   with every pair already computed, so nothing is measured twice.
1. :func:`build_overlap_matrix` — score every ordered pair ``(i, j)`` (bottom of
   ``i`` vs top of ``j``) after cropping each frame's luma to its
   :class:`core.chrome.ContentSlice`. ``N`` is small (typically < 50) so the full
   ``N*(N-1)`` matrix is affordable when needed; it lets ordering see every
   candidate edge.
2. :func:`order_frames` — greedily assemble a Hamiltonian-ish path from the valid
   directed edges (each screenshot has ~one true successor and ~one true
   predecessor), joining any leftover sub-chains, then walking a **retry ladder**
   (:func:`core.offset.relaxed_vertical_offset`) on every adjacency that did not
   already validate. Adjacencies that never validate are reported as *fatal* gaps
   (the caller decides whether to halt); adjacencies recovered only by relaxing
   are reported as non-fatal low-confidence joins.

Coordinate reminder (CONTRACT.md): ``vertical_offset(A, B).overlap`` is the number
of rows such that ``A[H_A-overlap:H_A] ≈ B[0:overlap]``, with ``A`` / ``B`` already
cropped to their content slices. Timestamps are ONLY a tie-breaker/prior here,
never authoritative.

Pure stdlib + numpy. No cv2 / scipy / torch.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from core.offset import OverlapResult, relaxed_vertical_offset, vertical_offset

__all__ = ["build_overlap_matrix", "order_frames", "order_frames_chain_first"]

# Rank labels so the weakest adjacency can drive the overall confidence.
_CONF_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}
_RANK_CONF = {3: "high", 2: "medium", 1: "low", 0: "low"}

# Retry-ladder rungs tried (in order) on an adjacency that did not validate at the
# default level. Rung 0 == vertical_offset defaults, already captured in the
# matrix, so escalation starts at rung 1.
_LADDER_LEVELS = (1, 2, 3)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _content_gray(frame: Any, slc: Any) -> np.ndarray:
    """Return ``frame``'s luma cropped to its content slice ``[top:bottom]``."""
    gray = np.asarray(frame.gray, dtype=np.float32)
    top = max(0, int(slc.top))
    bottom = min(gray.shape[0], int(slc.bottom))
    if bottom <= top:
        return gray  # degenerate slice; fall back to the whole frame
    return gray[top:bottom]


def _ts(frame: Any) -> float | None:
    """Return ``frame.ts`` (epoch seconds) or ``None`` when absent."""
    ts = getattr(frame, "ts", None)
    return None if ts is None else float(ts)


# --------------------------------------------------------------------------- #
# pairwise overlap matrix
# --------------------------------------------------------------------------- #
def build_overlap_matrix(
    frames: list[Any],
    slices: list[Any],
    known: dict[tuple[int, int], OverlapResult] | None = None,
) -> dict[tuple[int, int], OverlapResult]:
    """Score every ordered content-frame pair ``(i, j)`` (bottom of i vs top of j).

    Returns a dict keyed ``(i, j)`` (``i != j``) whose value is the
    :class:`~core.offset.OverlapResult` of ``vertical_offset`` between frame ``i``'s
    content and frame ``j``'s content. Pairs already present in ``known`` (e.g. the
    consecutive pairs a chain-first probe measured) are carried over untouched
    instead of being recomputed.
    """
    grays = [_content_gray(f, s) for f, s in zip(frames, slices)]
    n = len(frames)
    matrix: dict[tuple[int, int], OverlapResult] = dict(known) if known else {}
    for i in range(n):
        for j in range(n):
            if i == j or (i, j) in matrix:
                continue
            matrix[(i, j)] = vertical_offset(grays[i], grays[j])
    return matrix


# --------------------------------------------------------------------------- #
# union-find (cycle guard while greedily linking edges)
# --------------------------------------------------------------------------- #
class _DSU:
    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:  # path compression
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        self._parent[self.find(a)] = self.find(b)


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #
def _greedy_chains(
    n: int,
    matrix: dict[tuple[int, int], OverlapResult],
    ts: list[float | None],
    use_ts: bool,
) -> list[list[int]]:
    """Link valid directed edges into disjoint chains (each node <=1 succ/pred).

    Edges are consumed strongest-first; an edge is accepted only when its source
    still lacks a successor, its target still lacks a predecessor, and it would not
    close a cycle. Timestamps only break exact score ties.
    """
    edges: list[tuple[float, int, int, int]] = []
    for (i, j), res in matrix.items():
        if not res.valid:
            continue
        ts_flag = 0
        if use_ts and ts[i] is not None and ts[j] is not None:
            ts_flag = 0 if ts[i] <= ts[j] else 1  # prefer ts-consistent on ties
        edges.append((res.score, i, j, ts_flag))

    # Strongest score first; ts-consistent first on exact ties.
    edges.sort(key=lambda e: (-e[0], e[3]))

    succ: dict[int, int] = {}
    pred: dict[int, int] = {}
    dsu = _DSU(n)
    for _score, i, j, _flag in edges:
        if i in succ or j in pred:
            continue
        if dsu.find(i) == dsu.find(j):
            continue  # would form a cycle
        succ[i] = j
        pred[j] = i
        dsu.union(i, j)

    heads = [i for i in range(n) if i not in pred]
    chains: list[list[int]] = []
    for head in heads:
        chain = [head]
        while chain[-1] in succ:
            chain.append(succ[chain[-1]])
        chains.append(chain)
    return chains


def _chain_ts(chain: list[int], ts: list[float | None]) -> float:
    """Representative timestamp of a chain (earliest known ts, else +inf)."""
    known = [ts[i] for i in chain if ts[i] is not None]
    return min(known) if known else float("inf")


def _join_chains(
    chains: list[list[int]],
    matrix: dict[tuple[int, int], OverlapResult],
    ts: list[float | None],
    use_ts: bool,
) -> list[int]:
    """Greedily concatenate sub-chains tail->head into one order.

    The join edge is chosen by best available pairwise score (even an *invalid*
    one — those become gaps that the retry ladder re-examines). Timestamps break
    ties and seed the very first pick so a fully-disconnected batch still lands in
    a stable, ts-consistent order.
    """
    if not chains:
        return []
    remaining = [list(c) for c in chains]

    # Seed with the ts-earliest chain (stable, deterministic starting point).
    remaining.sort(key=lambda c: _chain_ts(c, ts))
    order = remaining.pop(0)

    while remaining:
        best_idx = 0
        best_key = None
        for idx, cand in enumerate(remaining):
            res = matrix.get((order[-1], cand[0]))
            score = res.score if res is not None else float("-inf")
            ts_flag = 0
            if use_ts:
                t_tail, t_head = ts[order[-1]], _chain_ts(cand, ts)
                if t_tail is not None and t_head != float("inf"):
                    ts_flag = 0 if t_tail <= t_head else 1
            key = (-score, ts_flag, _chain_ts(cand, ts))
            if best_key is None or key < best_key:
                best_key = key
                best_idx = idx
        order.extend(remaining.pop(best_idx))
    return order


def _validate_adjacencies(
    order: list[int],
    frames: list[Any],
    slices: list[Any],
    matrix: dict[tuple[int, int], OverlapResult],
) -> tuple[list[dict], int]:
    """Retry-ladder every adjacency; collect gaps and the weakest confidence rank.

    A valid matrix edge stands as-is. An invalid one is escalated through
    :func:`relaxed_vertical_offset` levels 1..3; the first level that validates
    replaces the matrix entry and yields a non-fatal low-confidence join. If none
    validate, a fatal gap is recorded (matrix entry left invalid).
    """
    gaps: list[dict] = []
    weakest = _CONF_RANK["high"]

    for a, b in zip(order, order[1:]):
        res = matrix.get((a, b))
        if res is not None and res.valid:
            weakest = min(weakest, _CONF_RANK.get(res.confidence, 1))
            continue

        grayA = _content_gray(frames[a], slices[a])
        grayB = _content_gray(frames[b], slices[b])
        recovered: OverlapResult | None = None
        for level in _LADDER_LEVELS:
            relaxed = relaxed_vertical_offset(grayA, grayB, level)
            if relaxed.valid:
                recovered = relaxed
                break

        if recovered is not None:
            matrix[(a, b)] = recovered  # so stitch/compute_splices see the overlap
            gaps.append({"between": (a, b), "fatal": False, "confidence": "low"})
            weakest = min(weakest, _CONF_RANK["low"])
        else:
            gaps.append(
                {"between": (a, b), "fatal": True, "reason": "no overlap anchor"}
            )
            weakest = min(weakest, _CONF_RANK["none"])

    return gaps, weakest


def order_frames(
    frames: list[Any],
    slices: list[Any],
    matrix: dict[tuple[int, int], OverlapResult],
    use_ts: bool = True,
) -> dict:
    """Reconstruct the frame order from pairwise overlap scores.

    Returns ``{"order": [idx...], "gaps": [...], "confidence": str,
    "reordered": bool, "matrix": matrix}``. ``order`` is the reconstructed
    top-to-bottom sequence of input indices. ``gaps`` lists adjacencies that only
    validated after relaxation (``fatal: False``) or never validated
    (``fatal: True``); the caller decides whether a fatal gap halts the pipeline.
    ``confidence`` reflects the weakest adjacency. Never raises on a fatal gap.
    """
    n = len(frames)
    if n == 0:
        return {"order": [], "gaps": [], "confidence": "high",
                "reordered": False, "matrix": matrix}
    if n == 1:
        return {"order": [0], "gaps": [], "confidence": "high",
                "reordered": False, "matrix": matrix}

    ts = [_ts(f) for f in frames]
    chains = _greedy_chains(n, matrix, ts, use_ts)
    order = _join_chains(chains, matrix, ts, use_ts)

    gaps, weakest = _validate_adjacencies(order, frames, slices, matrix)

    confidence = _RANK_CONF[weakest]
    reordered = order != list(range(n))
    return {
        "order": order,
        "gaps": gaps,
        "confidence": confidence,
        "reordered": reordered,
        "matrix": matrix,
    }


def order_frames_chain_first(
    frames: list[Any],
    slices: list[Any],
    use_ts: bool = True,
) -> dict:
    """Reconstruct frame order, trusting-but-verifying the capture order first.

    Same-device captures (sorted screenshot filenames, video keyframes) almost
    always arrive top-to-bottom already, so the ``N*(N-1)`` matrix is wasted work
    in the common case. This entry point verifies the cheap hypotheses first and
    escalates only on evidence:

    1. **Forward chain** — score only the ``n-1`` consecutive pairs. All valid →
       accept the input order (``strategy: "chain"``).
    2. **Reversed chain** — if most forward pairs failed, the batch may simply be
       newest-first; probe the ``n-1`` reversed pairs (``strategy:
       "chain-reversed"``). Still O(n).
    3. **Retry ladder** — if most forward pairs validated, the order is right and
       the stragglers are hard pairs (blur, sparse content): escalate just those
       through :func:`relaxed_vertical_offset`. Recovered ones become non-fatal
       low-confidence joins, exactly as in :func:`order_frames`.
    4. **Full-matrix fallback** — anything else (shuffled batch, or a dead
       adjacency that could mean mis-ordering) gets the complete pairwise matrix
       and :func:`order_frames`, which alone may declare *fatal* gaps. The matrix
       is seeded with every pair already computed above (``strategy:
       "matrix-fallback"``).

    Returns the same dict as :func:`order_frames` plus a ``"strategy"`` key. On
    the chain paths ``matrix`` is sparse (adjacencies only) — sufficient for
    every downstream consumer, which read only consecutive-pair entries.
    """
    n = len(frames)
    if n == 0:
        return {"order": [], "gaps": [], "confidence": "high",
                "reordered": False, "matrix": {}, "strategy": "chain"}
    if n == 1:
        return {"order": [0], "gaps": [], "confidence": "high",
                "reordered": False, "matrix": {}, "strategy": "chain"}

    grays = [_content_gray(f, s) for f, s in zip(frames, slices)]
    matrix: dict[tuple[int, int], OverlapResult] = {}

    forward = list(zip(range(n), range(1, n)))
    for i, j in forward:
        matrix[(i, j)] = vertical_offset(grays[i], grays[j])
    forward_valid = sum(1 for p in forward if matrix[p].valid)

    def _chain_result(order: list[int], strategy: str) -> dict:
        gaps, weakest = _validate_adjacencies(order, frames, slices, matrix)
        return {
            "order": order,
            "gaps": gaps,
            "confidence": _RANK_CONF[weakest],
            "reordered": order != list(range(n)),
            "matrix": matrix,
            "strategy": strategy,
        }

    if forward_valid == n - 1:
        return _chain_result(list(range(n)), "chain")

    if forward_valid < (n - 1) / 2:
        # Mostly invalid forward: probe a newest-first batch before going O(n^2).
        reverse = [(k + 1, k) for k in range(n - 1)]
        for i, j in reverse:
            matrix[(i, j)] = vertical_offset(grays[i], grays[j])
        if all(matrix[p].valid for p in reverse):
            return _chain_result(list(range(n - 1, -1, -1)), "chain-reversed")
    else:
        # Mostly valid forward: the order is right; ladder just the stragglers.
        # A remaining fatal adjacency might instead mean local mis-ordering, so
        # that case falls through to the full matrix rather than being declared.
        gaps, weakest = _validate_adjacencies(list(range(n)), frames, slices, matrix)
        if not any(g.get("fatal") for g in gaps):
            return {
                "order": list(range(n)),
                "gaps": gaps,
                "confidence": _RANK_CONF[weakest],
                "reordered": False,
                "matrix": matrix,
                "strategy": "chain",
            }

    full = build_overlap_matrix(frames, slices, known=matrix)
    result = order_frames(frames, slices, full, use_ts=use_ts)
    result["strategy"] = "matrix-fallback"
    return result
