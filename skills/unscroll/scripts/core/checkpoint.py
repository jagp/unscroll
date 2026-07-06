"""core/checkpoint.py — resumable pipeline manifest.

A :class:`Manifest` records, per pipeline *stage*, the output reference produced
for a given input *key*.  A stage keys itself by a content hash of its inputs +
params (see :func:`hash_inputs`); before recomputing it asks the manifest whether
that ``(stage, key)`` was already produced.  If so it reuses the stored
``output_ref`` (a path on disk) and skips the work, making the whole pipeline
idempotent and resumable across interrupted runs.

The manifest is persisted as a single JSON file in the workdir and written
atomically (temp file + :func:`os.replace`) so a crash mid-write never corrupts a
previously-good manifest.

Pure stdlib (``json``, ``hashlib``, ``os``).  No third-party dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os

__all__ = ["Manifest", "hash_inputs"]

_MANIFEST_NAME = "manifest.json"
_VERSION = 1
_HASH_LEN = 16


def hash_inputs(*parts) -> str:
    """Return a stable 16-char sha256 hex over the given input parts.

    Accepts ``bytes``, ``str``, ``int``, and (possibly nested) lists/tuples of
    those.  Parts are fed to the hash with type-tagged, length-delimited framing
    so that neither concatenation ambiguity (``["a", "b"]`` vs ``["ab"]``) nor a
    type coincidence (``1`` vs ``"1"``) can collide.
    """
    h = hashlib.sha256()
    for part in parts:
        _feed(h, part)
    return h.hexdigest()[:_HASH_LEN]


def _feed(h: "hashlib._Hash", part) -> None:
    """Feed one part into the hash with unambiguous, type-tagged framing."""
    if isinstance(part, bytes):
        h.update(b"b:")
        h.update(str(len(part)).encode("utf-8"))
        h.update(b":")
        h.update(part)
    elif isinstance(part, bool):
        # bool is an int subclass; tag it distinctly so True != 1.
        h.update(b"o:")
        h.update(b"1" if part else b"0")
    elif isinstance(part, int):
        h.update(b"i:")
        h.update(str(part).encode("utf-8"))
        h.update(b";")
    elif isinstance(part, str):
        data = part.encode("utf-8")
        h.update(b"s:")
        h.update(str(len(data)).encode("utf-8"))
        h.update(b":")
        h.update(data)
    elif isinstance(part, (list, tuple)):
        h.update(b"l:")
        h.update(str(len(part)).encode("utf-8"))
        h.update(b"[")
        for item in part:
            _feed(h, item)
        h.update(b"]")
    elif part is None:
        h.update(b"n:")
    else:
        # Fallback: stringify anything else deterministically.
        data = str(part).encode("utf-8")
        h.update(b"x:")
        h.update(str(len(data)).encode("utf-8"))
        h.update(b":")
        h.update(data)


class Manifest:
    """A resumable pipeline manifest persisted as JSON in a workdir."""

    def __init__(self, workdir: str, stages: dict | None = None) -> None:
        self.workdir = workdir
        # stages: {stage_name: {key: output_ref}}
        self.stages: dict[str, dict[str, str]] = stages or {}

    @property
    def path(self) -> str:
        """Absolute path to the manifest JSON inside the workdir."""
        return os.path.join(self.workdir, _MANIFEST_NAME)

    @classmethod
    def load(cls, workdir: str) -> "Manifest":
        """Load the manifest from ``workdir`` (return a fresh one if none exists)."""
        path = os.path.join(workdir, _MANIFEST_NAME)
        if not os.path.exists(path):
            return cls(workdir)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # A corrupt/unreadable manifest is treated as absent, not fatal.
            return cls(workdir)
        stages = data.get("stages") if isinstance(data, dict) else None
        if not isinstance(stages, dict):
            stages = {}
        return cls(workdir, stages)

    def save(self) -> None:
        """Atomically persist the manifest (temp file + os.replace)."""
        os.makedirs(self.workdir, exist_ok=True)
        payload = {"version": _VERSION, "stages": self.stages}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def cached(self, stage: str, key: str) -> str | None:
        """Return the stored output_ref for ``(stage, key)`` or ``None``."""
        return self.stages.get(stage, {}).get(key)

    def record(self, stage: str, key: str, output_ref: str) -> None:
        """Store ``output_ref`` for ``(stage, key)`` and persist the manifest."""
        self.stages.setdefault(stage, {})[key] = output_ref
        self.save()


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        m = Manifest.load(d)
        k = hash_inputs(["abc", 1], b"params")
        print("cached before:", m.cached("chrome", k))
        m.record("chrome", k, os.path.join(d, "out.png"))
        print("cached after:", Manifest.load(d).cached("chrome", k))
