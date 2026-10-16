"""Image helpers (Pillow): contact sheets for cheap overviews, Instagram-ready JPEGs."""

from __future__ import annotations

import base64
import io
from collections.abc import Sequence
from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

MAX_SHEET_PHOTOS = 30
MAX_PIXELS = 60_000_000  # refuse decompression bombs
PILImage.MAX_IMAGE_PIXELS = MAX_PIXELS


def _font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has a fixed-size default font
        return ImageFont.load_default()


def contact_sheet(
    previews: Sequence[bytes | None], columns: int = 5, cell: int = 360, quality: int = 82
) -> bytes:
    """One JPEG with numbered thumbnails (1..N). A missing/undecodable preview shows an X."""
    columns = max(1, min(columns, 8))
    cell = max(160, min(cell, 480))
    rows = -(-len(previews) // columns)
    pad = 6
    sheet = PILImage.new("RGB", (columns * (cell + pad) + pad, rows * (cell + pad) + pad), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = _font(max(14, cell // 12))
    for index, data in enumerate(previews):
        x = pad + (index % columns) * (cell + pad)
        y = pad + (index // columns) * (cell + pad)
        try:
            if data is None:
                raise ValueError("no preview")
            with PILImage.open(io.BytesIO(data)) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img = ImageOps.contain(img, (cell, cell))
            sheet.paste(img, (x + (cell - img.width) // 2, y + (cell - img.height) // 2))
        except (OSError, ValueError, PILImage.DecompressionBombError):
            draw.rectangle((x, y, x + cell, y + cell), outline=(160, 60, 60), width=2)
            draw.line((x, y, x + cell, y + cell), fill=(160, 60, 60), width=2)
            draw.line((x, y + cell, x + cell, y), fill=(160, 60, 60), width=2)
        label = str(index + 1)
        box = draw.textbbox((x + 4, y + 4), label, font=font)
        draw.rectangle((box[0] - 4, box[1] - 2, box[2] + 4, box[3] + 2), fill=(0, 0, 0))
        draw.text((x + 4, y + 4), label, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=quality)
    return out.getvalue()


def instagram_jpeg(data: bytes, max_edge: int = 1440, limit_bytes: int = 7_500_000) -> bytes:
    """Re-encode to a JPEG that fits the MHPhotos Instagram queue (8 MB), longest edge capped."""
    with PILImage.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((max_edge, max_edge), PILImage.Resampling.LANCZOS)
        for quality in (92, 88, 82, 76, 70, 60):
            out = io.BytesIO()
            img.save(out, "JPEG", quality=quality, optimize=True)
            if out.tell() <= limit_bytes:
                return out.getvalue()
    raise ValueError("image cannot be compressed below the size limit")


def downscale_jpeg(data: bytes, size: int = 768, quality: int = 85) -> bytes:
    """JPEG with the longest edge at most ``size`` px (previews for the chat)."""
    with PILImage.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((size, size), PILImage.Resampling.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=quality)
        return out.getvalue()


def thumb_png_b64(data: bytes) -> str:
    """8x8 PNG (base64) for the SocialHub duplicate check; the server computes the hash from it."""
    with PILImage.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img).convert("RGB").resize((8, 8), PILImage.Resampling.BOX)
        out = io.BytesIO()
        img.save(out, "PNG")
        return base64.b64encode(out.getvalue()).decode()


# ---- basic edits (a simple stand-in when Lightroom is not available) -------------------------
EDIT_RANGES: dict[str, tuple[float, float]] = {
    "exposure": (-2.0, 2.0),  # EV
    "contrast": (-50, 50),  # percent
    "saturation": (-50, 50),  # percent
    "warmth": (-50, 50),  # negative = cooler, positive = warmer
    "sharpen": (0, 100),  # percent
}
CROPS = {"original": None, "1:1": (1, 1), "4:5": (4, 5), "9:16": (9, 16), "16:9": (16, 9)}


def validate_adjust(ops: Any) -> tuple[dict[str, Any], str | None]:
    """Whitelist and range check for basic edits. Returns (clean, error)."""
    if ops is None:
        return {}, None
    if not isinstance(ops, dict) or not ops:
        return {}, "adjust must be an object like {\"exposure\": 0.3, \"crop\": \"4:5\"}"
    clean: dict[str, Any] = {}
    for key, value in ops.items():
        if key == "crop":
            if value not in CROPS:
                return {}, f"crop must be one of {', '.join(CROPS)}"
            clean[key] = value
        elif key in EDIT_RANGES:
            lo, hi = EDIT_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                return {}, f"{key} must be a number between {lo} and {hi}"
            clean[key] = value
        else:
            return {}, f"unsupported edit '{key}'. Allowed: {', '.join([*EDIT_RANGES, 'crop'])}"
    return clean, None


def adjust(data: bytes, ops: dict[str, Any], quality: int = 92) -> bytes:
    """Apply validated basic edits to a copy; the original bytes are never touched."""
    with PILImage.open(io.BytesIO(data)) as src:
        img = ImageOps.exif_transpose(src).convert("RGB")
    if ops.get("exposure"):
        img = ImageEnhance.Brightness(img).enhance(2 ** ops["exposure"])
    if ops.get("contrast"):
        img = ImageEnhance.Contrast(img).enhance(1 + ops["contrast"] / 100)
    if ops.get("saturation"):
        img = ImageEnhance.Color(img).enhance(1 + ops["saturation"] / 100)
    if ops.get("warmth"):
        k = ops["warmth"] / 100 * 0.25
        r, g, b = img.split()
        img = PILImage.merge(
            "RGB", (r.point(lambda v: min(255, v * (1 + k))), g, b.point(lambda v: min(255, v * (1 - k))))
        )
    if ops.get("sharpen"):
        img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=int(ops["sharpen"]), threshold=3))
    ratio = CROPS.get(ops.get("crop", "original"))
    if ratio:
        rw, rh = ratio
        w, h = img.size
        if w * rh > h * rw:  # too wide
            nw = h * rw // rh
            img = img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
        else:  # too tall
            nh = w * rh // rw
            img = img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def before_after(before: bytes, after: bytes, size: int = 640) -> bytes:
    """One image: original left, edited right (both fitted into size x size)."""
    pad = 8
    sheet = PILImage.new("RGB", (2 * size + 3 * pad, size + 2 * pad), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    font = _font(max(14, size // 22))
    for index, (label, data) in enumerate((("Vorher", before), ("Nachher", after))):
        with PILImage.open(io.BytesIO(data)) as img:
            img = ImageOps.contain(ImageOps.exif_transpose(img).convert("RGB"), (size, size))
        x = pad + index * (size + pad)
        sheet.paste(img, (x + (size - img.width) // 2, pad + (size - img.height) // 2))
        box = draw.textbbox((x + 4, pad + 4), label, font=font)
        draw.rectangle((box[0] - 4, box[1] - 2, box[2] + 4, box[3] + 2), fill=(0, 0, 0))
        draw.text((x + 4, pad + 4), label, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    sheet.save(out, "JPEG", quality=85)
    return out.getvalue()
