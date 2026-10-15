"""Image helpers (Pillow): contact sheets for cheap overviews, Instagram-ready JPEGs."""

from __future__ import annotations

import io
from collections.abc import Sequence

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont, ImageOps

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
