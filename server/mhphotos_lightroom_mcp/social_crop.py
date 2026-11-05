"""Crop rectangles for social formats (Instagram 4:5, Stories 9:16, ...), as Lightroom develop settings.

Lightroom's CropLeft/Right/Top/Bottom are fractions (0..1) of the uncropped, correctly rotated picture.
The crop keeps as much of the picture as the format allows and is centred on a focus point.
"""

from __future__ import annotations

ASPECTS: dict[str, tuple[int, int]] = {
    "4:5": (4, 5),      # Instagram feed, portrait (1080 x 1350)
    "9:16": (9, 16),    # Stories and Reels
    "1:1": (1, 1),      # square
    "3:4": (3, 4),
    "2:3": (2, 3),
    "16:9": (16, 9),
    "1.91:1": (191, 100),  # Instagram feed, landscape
}
FULL_FRAME = {"CropLeft": 0.0, "CropRight": 1.0, "CropTop": 0.0, "CropBottom": 1.0, "CropAngle": 0.0}


def crop_settings(width: int, height: int, aspect: str, focus_x: float = 0.5, focus_y: float = 0.5) -> dict[str, float]:
    """Develop settings that cut a width x height picture to ``aspect`` around (focus_x, focus_y), 0..1 each."""
    if aspect not in ASPECTS:
        raise ValueError(f"aspect must be one of: {', '.join(ASPECTS)}")
    if width <= 0 or height <= 0:
        raise ValueError("picture size must be positive")
    aw, ah = ASPECTS[aspect]
    target = aw / ah
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
