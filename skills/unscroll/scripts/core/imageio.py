"""Image intake primitives for unscroll.

Owns the :class:`Frame` data type and all raw pixel I/O used by the rest of the
pipeline. Images live in memory as RGB ``uint8`` arrays of shape ``(H, W, 3)``;
grayscale/luma is ``float32`` of shape ``(H, W)`` in the 0-255 range.

Standard library + numpy + Pillow only. HEIC/HEIF is decoded by shelling out to
the external ``ffmpeg`` binary (Pillow cannot decode it).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass

import numpy as np
from PIL import Image

__all__ = [
    "Frame",
    "load_image",
    "to_gray",
    "content_hash",
    "modal_color",
    "make_frame",
    "save_png",
]

# Extensions Pillow decodes natively.
_PIL_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
# Extensions that require an ffmpeg transcode to PNG first.
_FFMPEG_EXTS = frozenset({".heic", ".heif"})


@dataclass
class Frame:
    """A single normalized source image plus its derived metadata."""

    id: str            # 16-hex content hash of the normalized RGB bytes
    path: str          # source path on disk (normalized PNG)
    rgb: np.ndarray    # (H,W,3) uint8
    gray: np.ndarray   # (H,W) float32
    width: int
    height: int
    source: str        # "screenshot" | "video"
    index: int         # original intake index (opaque; NOT trusted for order)
    ts: float | None   # filesystem/frame timestamp (epoch seconds) or None


def _pil_to_rgb(img: Image.Image) -> np.ndarray:
    """Convert a Pillow image to a contiguous RGB uint8 ``(H, W, 3)`` array."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    return np.ascontiguousarray(np.asarray(img, dtype=np.uint8))


def _decode_heic(path: str) -> np.ndarray:
    """Decode a HEIC/HEIF file by transcoding to PNG via ffmpeg."""
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "frame.png")
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", path, out],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0 or not os.path.isfile(out):
            raise ValueError(
                f"ffmpeg failed to decode {path!r}: "
                f"{proc.stderr.decode('utf-8', 'replace')[-500:]}"
            )
        with Image.open(out) as img:
            return _pil_to_rgb(img)


def load_image(path: str) -> np.ndarray:
    """Decode an image file to an RGB uint8 ``(H, W, 3)`` array.

    PNG/JPG/JPEG/WEBP/BMP are decoded with Pillow; HEIC/HEIF are transcoded to
    PNG with ffmpeg first. Raises ``ValueError`` only if the file is genuinely
    undecodable.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in _FFMPEG_EXTS:
        return _decode_heic(path)
    try:
        with Image.open(path) as img:
            return _pil_to_rgb(img)
    except ValueError:
        raise
    except Exception as exc:
        # Unknown/misnamed extension: fall back to ffmpeg before giving up.
        try:
            return _decode_heic(path)
        except Exception:
            raise ValueError(f"could not decode image {path!r}: {exc}") from exc


def to_gray(rgb: np.ndarray) -> np.ndarray:
    """Return Rec.601 luma as float32 ``(H, W)`` in the 0-255 range."""
    r = rgb[..., 0].astype(np.float32)
    g = rgb[..., 1].astype(np.float32)
    b = rgb[..., 2].astype(np.float32)
    return 0.299 * r + 0.587 * g + 0.114 * b


def content_hash(x: bytes | np.ndarray) -> str:
    """Return the first 16 hex chars of the sha256 of ``x``'s raw bytes."""
    data = x.tobytes() if isinstance(x, np.ndarray) else x
    return hashlib.sha256(data).hexdigest()[:16]


def modal_color(rgb: np.ndarray) -> np.ndarray:
    """Return the modal (background) color as a ``(3,)`` uint8 array.

    Uses an 8-bit-binned histogram: each channel value is binned by
    ``value // 8``, the most common ``(r, g, b)`` bin is selected, and the bin
    center ``bin * 8 + 4`` is returned. Large images are subsampled for speed.
    """
    flat = rgb.reshape(-1, 3)
    # Subsample to at most ~200k pixels; the mode is stable under subsampling.
    max_samples = 200_000
    if flat.shape[0] > max_samples:
        step = flat.shape[0] // max_samples + 1
        flat = flat[::step]
    bins = (flat // 8).astype(np.int64)  # (N,3), each in [0,31]
    # Pack the three 5-bit bin indices into one integer key.
    keys = (bins[:, 0] << 10) | (bins[:, 1] << 5) | bins[:, 2]
    values, counts = np.unique(keys, return_counts=True)
    top = int(values[int(np.argmax(counts))])
    rb = (top >> 10) & 0x1F
    gb = (top >> 5) & 0x1F
    bb = top & 0x1F
    return (np.array([rb, gb, bb], dtype=np.int64) * 8 + 4).astype(np.uint8)


def make_frame(
    path: str,
    source: str,
    index: int,
    ts: float | None = None,
) -> Frame:
    """Load ``path`` and build a fully populated :class:`Frame`."""
    rgb = load_image(path)
    gray = to_gray(rgb)
    height, width = rgb.shape[0], rgb.shape[1]
    return Frame(
        id=content_hash(rgb),
        path=path,
        rgb=rgb,
        gray=gray,
        width=int(width),
        height=int(height),
        source=source,
        index=int(index),
        ts=ts,
    )


def save_png(rgb: np.ndarray, path: str) -> None:
    """Write an RGB uint8 ``(H, W, 3)`` array to ``path`` as a PNG."""
    arr = np.ascontiguousarray(rgb.astype(np.uint8, copy=False))
    Image.fromarray(arr, mode="RGB").save(path, format="PNG")


if __name__ == "__main__":  # pragma: no cover - manual smoke demo
    demo = np.zeros((32, 48, 3), dtype=np.uint8)
    demo[:] = (240, 240, 245)
    print("hash:", content_hash(demo))
    print("modal:", modal_color(demo))
    print("gray dtype:", to_gray(demo).dtype, to_gray(demo).shape)
