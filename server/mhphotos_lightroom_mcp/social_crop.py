"""Crop rectangles for social formats (Instagram 4:5, Stories 9:16, ...), as Lightroom develop settings.

Lightroom's CropLeft/Right/Top/Bottom are fractions (0..1) of the uncropped, correctly rotated picture.
The crop keeps as much of the picture as the format allows and is centred on a focus point.
"""

from __future__ import annotations

import math

ASPECTS: dict[str, tuple[int, int]] = {
    "4:5": (4, 5),      # Instagram feed, portrait (1080 x 1350)
    "9:16": (9, 16),    # Stories and Reels
    "1:1": (1, 1),      # square
    "3:4": (3, 4),
    "2:3": (2, 3),
    "16:9": (16, 9),
    "1.91:1": (191, 100),  # Instagram feed, landscape
}
ORIGINAL = "original"  # keep the shape of the picture (used to straighten a horizon only)
FULL_FRAME = {"CropLeft": 0.0, "CropRight": 1.0, "CropTop": 0.0, "CropBottom": 1.0, "CropAngle": 0.0}


def straight_crop(width: int, height: int, angle: float, target: float) -> dict[str, float]:
    """Largest centred crop of shape ``target`` (width / height) that stays inside the picture turned by ``angle`` degrees."""
    rad = math.radians(abs(angle))
    c, s = math.cos(rad), math.sin(rad)
    k = min(width / (target * c + s), height / (target * s + c))  # crop height in pixels
    frac_w, frac_h = k * target / width, k / height
    left, top = (1 - frac_w) / 2, (1 - frac_h) / 2
    return {"CropLeft": round(left, 6), "CropRight": round(left + frac_w, 6), "CropTop": round(top, 6),
            "CropBottom": round(top + frac_h, 6), "CropAngle": float(angle)}


def crop_settings(width: int, height: int, aspect: str, focus_x: float = 0.5, focus_y: float = 0.5,
                  angle: float = 0.0) -> dict[str, float]:
    """Develop settings that cut a width x height picture to ``aspect`` around (focus_x, focus_y), 0..1 each.

    With ``angle`` (degrees, -45..45) the picture is also turned to level a horizon; the crop is then centred and as
    large as fits without empty corners. aspect "original" keeps the shape of the picture.
    """
    if aspect not in ASPECTS and aspect != ORIGINAL:
        raise ValueError(f"aspect must be one of: {', '.join([*ASPECTS, ORIGINAL])}")
    if width <= 0 or height <= 0:
        raise ValueError("picture size must be positive")
    if not -45 <= angle <= 45:
        raise ValueError("angle must be between -45 and 45 degrees")
    if aspect == ORIGINAL:
        target = width / height
    else:
        aw, ah = ASPECTS[aspect]
        target = aw / ah
    if angle:
        return straight_crop(width, height, angle, target)
    focus_x = min(1.0, max(0.0, focus_x))
    focus_y = min(1.0, max(0.0, focus_y))
    left, right, top, bottom = 0.0, 1.0, 0.0, 1.0
    if width / height > target:  # too wide: cut the sides
        frac = (height * target) / width
        left = min(max(focus_x - frac / 2, 0.0), 1.0 - frac)
        right = left + frac
    elif width / height < target:  # too tall: cut top and bottom
        frac = (width / target) / height
        top = min(max(focus_y - frac / 2, 0.0), 1.0 - frac)
        bottom = top + frac
    return {"CropLeft": round(left, 6), "CropRight": round(right, 6), "CropTop": round(top, 6),
            "CropBottom": round(bottom, 6), "CropAngle": 0.0}
