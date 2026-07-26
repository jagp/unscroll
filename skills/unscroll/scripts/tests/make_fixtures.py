"""Synthetic chat-fixture generator shared across unscroll tests.

Renders deterministic, iOS-style fake chat images and derives overlapping
"screenshots" and short scroll videos from them, so every module can exercise
chrome-masking, overlap detection, splicing, segmentation and video keyframing
against ground truth without real user data.

Standard library + numpy + Pillow only, plus the external ``ffmpeg`` binary for
video synthesis.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "MessageMeta",
    "make_chat_image",
    "make_chat_with_meta",
    "slice_overlapping",
    "slice_overlapping_with_truth",
    "make_scroll_video",
]

# --- palette (iOS light mode-ish) -------------------------------------------
_BG = (255, 255, 255)
_STATUS_BAR = (248, 248, 248)
_INPUT_BAR = (245, 245, 247)
_OTHER_BUBBLE = (229, 229, 234)   # gray, left
_SELF_BUBBLE = (10, 132, 255)     # blue, right
_OTHER_TEXT = (0, 0, 0)
_SELF_TEXT = (255, 255, 255)

_STATUS_H = 44
_INPUT_H = 52
_BUBBLE_PAD_X = 12
_BUBBLE_PAD_Y = 8
_SIDE_MARGIN = 12
_MAX_BUBBLE_FRAC = 0.72

_WORDS = (
    "hey there how are you doing today okay sure sounds good let me know "
    "when you get a chance i think we should meet later maybe around noon "
    "did you see that thing yesterday it was wild lol yeah totally agree "
    "call me whenever thanks so much appreciate it no worries all good"
).split()


@dataclass
class MessageMeta:
    """Ground-truth location and attribution of one rendered bubble."""

    top: int
    bottom: int
    sender: str   # "self" | "other"
    text: str


def _load_font(size: int) -> ImageFont.ImageFont:
    """Return a truetype font if available, else Pillow's bitmap default."""
    for name in ("arial.ttf", "DejaVuSans.ttf", "Helvetica.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(text: str, font: ImageFont.ImageFont, max_w: int,
          draw: ImageDraw.ImageDraw) -> list[str]:
    """Greedy word-wrap ``text`` to fit ``max_w`` pixels."""
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def make_chat_with_meta(
    n_messages: int = 20,
    width: int = 390,
    msg_gap: int = 18,
    seed: int = 0,
) -> tuple[np.ndarray, list[MessageMeta]]:
    """Render a fake chat as one tall RGB image plus per-bubble ground truth.

    The image has a near-constant top status-bar band and bottom input-bar band
    (chrome), a light background, left-aligned gray "other" bubbles and
    right-aligned blue "self" bubbles separated by clear whitespace gutters.
    Deterministic for a given ``seed``.
    """
    rng = np.random.default_rng(seed)
    font = _load_font(17)
    line_h = 22
    max_bubble_w = int(width * _MAX_BUBBLE_FRAC)

    # Probe canvas to measure text before we know the final height.
    probe = ImageDraw.Draw(Image.new("RGB", (width, 10)))

    # Pre-compute each bubble's wrapped lines and pixel geometry.
    plans: list[dict] = []
    y = _STATUS_H + msg_gap
    for i in range(n_messages):
        sender = "self" if rng.random() < 0.5 else "other"
        n_words = int(rng.integers(2, 14))
        start = int(rng.integers(0, len(_WORDS)))
        text = " ".join(_WORDS[(start + k) % len(_WORDS)] for k in range(n_words))
        text_max = max_bubble_w - 2 * _BUBBLE_PAD_X
        lines = _wrap(text, font, text_max, probe)
        text_w = max(int(probe.textlength(ln, font=font)) for ln in lines)
        bub_w = text_w + 2 * _BUBBLE_PAD_X
        bub_h = line_h * len(lines) + 2 * _BUBBLE_PAD_Y
        plans.append(
            {"sender": sender, "text": text, "lines": lines,
             "w": bub_w, "h": bub_h, "top": y}
        )
        y += bub_h + msg_gap

    total_h = y + _INPUT_H

    img = Image.new("RGB", (width, total_h), _BG)
    draw = ImageDraw.Draw(img)

    meta: list[MessageMeta] = []
    for p in plans:
        top, bub_w, bub_h = p["top"], p["w"], p["h"]
        if p["sender"] == "self":
            x0 = width - _SIDE_MARGIN - bub_w
            bubble, txt_color = _SELF_BUBBLE, _SELF_TEXT
        else:
            x0 = _SIDE_MARGIN
            bubble, txt_color = _OTHER_BUBBLE, _OTHER_TEXT
        x1, y1 = x0 + bub_w, top + bub_h
        draw.rounded_rectangle([x0, top, x1, y1], radius=16, fill=bubble)
        ty = top + _BUBBLE_PAD_Y
        for ln in p["lines"]:
            draw.text((x0 + _BUBBLE_PAD_X, ty), ln, font=font, fill=txt_color)
            ty += line_h
        meta.append(MessageMeta(top=top, bottom=y1, sender=p["sender"],
                                text=p["text"]))

    # Chrome bands: near-constant color, drawn last so they always sit on top.
    draw.rectangle([0, 0, width, _STATUS_H], fill=_STATUS_BAR)
    draw.rectangle([0, total_h - _INPUT_H, width, total_h], fill=_INPUT_BAR)

    return np.asarray(img, dtype=np.uint8), meta


def make_chat_image(
    n_messages: int = 20,
    width: int = 390,
    msg_gap: int = 18,
    seed: int = 0,
) -> np.ndarray:
    """Render a fake chat as one tall RGB image (metadata discarded)."""
    img, _ = make_chat_with_meta(n_messages, width, msg_gap, seed)
    return img


def slice_overlapping_with_truth(
    tall_img: np.ndarray,
    n_shots: int = 5,
    overlap_frac: float = 0.3,
    jitter: int = 0,
    seed: int = 0,
) -> tuple[list[np.ndarray], list[int]]:
    """Cut ``tall_img`` into ``n_shots`` vertically overlapping screenshots.

    Each screenshot re-includes the status-bar and input-bar chrome bands so
    chrome masking can be exercised, and consecutive shots share a known number
    of content rows. Returns ``(shots, overlaps)`` where ``overlaps[k]`` is the
    true number of shared content rows between shot ``k`` and shot ``k+1``.
    """
    if not 0.0 <= overlap_frac < 1.0:
        raise ValueError("overlap_frac must be in [0, 1)")
    if n_shots < 1:
        raise ValueError("n_shots must be >= 1")

    rng = np.random.default_rng(seed)
    H = tall_img.shape[0]
    status = tall_img[:_STATUS_H]
    inputbar = tall_img[H - _INPUT_H:]
    content = tall_img[_STATUS_H:H - _INPUT_H]
    C = content.shape[0]

    if n_shots == 1:
        shot = np.concatenate([status, content, inputbar], axis=0)
        return [shot], []

    # Window over content with a fixed step so neighbours overlap by overlap_frac.
    win = int(round(C / (n_shots - (n_shots - 1) * overlap_frac)))
    win = max(win, int(C // n_shots) + 1)
    win = min(win, C)
    step = max(1, int(round(win * (1.0 - overlap_frac))))

    starts: list[int] = []
    for k in range(n_shots):
        s = k * step
        if jitter:
            s += int(rng.integers(-jitter, jitter + 1))
        s = int(np.clip(s, 0, max(0, C - win)))
        starts.append(s)
    # Guarantee the last window reaches the bottom of the content.
    starts[-1] = max(starts[-1], C - win)
    starts[-1] = int(np.clip(starts[-1], 0, max(0, C - win)))

    shots: list[np.ndarray] = []
    windows: list[tuple[int, int]] = []
    for s in starts:
        e = min(s + win, C)
        body = content[s:e]
        shot = np.concatenate([status, body, inputbar], axis=0)
        shots.append(np.ascontiguousarray(shot))
        windows.append((s, e))

    overlaps: list[int] = []
    for k in range(n_shots - 1):
        s0, e0 = windows[k]
        s1, e1 = windows[k + 1]
        overlaps.append(max(0, min(e0, e1) - max(s0, s1)))

    return shots, overlaps


def slice_overlapping(
    tall_img: np.ndarray,
    n_shots: int = 5,
    overlap_frac: float = 0.3,
    jitter: int = 0,
    seed: int = 0,
) -> list[np.ndarray]:
    """Cut ``tall_img`` into overlapping screenshots (truth discarded)."""
    shots, _ = slice_overlapping_with_truth(
        tall_img, n_shots, overlap_frac, jitter, seed
    )
    return shots


def _motion_blur_rows(frame: np.ndarray, k: int = 9) -> np.ndarray:
    """Apply a cheap vertical box blur to simulate scroll motion blur."""
    f = frame.astype(np.float32)
    acc = np.zeros_like(f)
    half = k // 2
    for d in range(-half, half + 1):
        acc += np.roll(f, d, axis=0)
    return np.clip(acc / k, 0, 255).astype(np.uint8)


def make_scroll_video(
    tall_img: np.ndarray,
    path: str,
    fps: int = 30,
    scroll_px_per_frame: int = 12,
    add_blur: bool = True,
) -> None:
    """Synthesize a short MP4 that pans down ``tall_img`` using ffmpeg.

    PNG frames are rendered with numpy/Pillow (a fixed-height viewport sliding
    down the tall image) and then encoded with ``ffmpeg -framerate ... -i
    frame_%05d.png``. When ``add_blur`` is set, a couple of frames get a vertical
    motion blur to mimic fast scrolling. Raises ``RuntimeError`` if ffmpeg fails.
    """
    H, W, _ = tall_img.shape
    view_h = min(H, max(64, H // 3))
    view_h -= view_h % 2  # keep even for yuv420p
    if view_h < 2:
        view_h = 2
    max_top = H - view_h
    step = max(1, int(scroll_px_per_frame))
    tops = list(range(0, max_top + 1, step))
    if not tops or tops[-1] != max_top:
        tops.append(max_top)

    blur_at = set()
    if add_blur and len(tops) >= 4:
        blur_at = {len(tops) // 3, 2 * len(tops) // 3}

    with tempfile.TemporaryDirectory() as tmp:
        for i, top in enumerate(tops):
            view = tall_img[top:top + view_h]
            if i in blur_at:
                view = _motion_blur_rows(view)
            Image.fromarray(np.ascontiguousarray(view), mode="RGB").save(
                os.path.join(tmp, f"frame_{i:05d}.png")
            )
        out = os.path.abspath(path)
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-framerate", str(fps),
                "-i", os.path.join(tmp, "frame_%05d.png"),
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                out,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if proc.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError(
                "ffmpeg failed to encode scroll video: "
                + proc.stderr.decode("utf-8", "replace")[-800:]
            )


if __name__ == "__main__":  # pragma: no cover - manual smoke demo
    img, meta = make_chat_with_meta(n_messages=12, seed=1)
    print("chat image:", img.shape, "messages:", len(meta))
    shots, overlaps = slice_overlapping_with_truth(img, n_shots=4, overlap_frac=0.3)
    print("shots:", [s.shape for s in shots])
    print("overlaps:", overlaps)
