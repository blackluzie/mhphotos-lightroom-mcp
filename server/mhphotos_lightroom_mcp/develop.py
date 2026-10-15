"""Whitelist and range check for Lightroom develop settings (Process Version 2012 names).

Claude proposes edits as a dict of these parameters. Unknown keys or out-of-range values are
rejected before anything reaches Lightroom.
"""

from __future__ import annotations

from typing import Any

_HSL_COLORS = ("Red", "Orange", "Yellow", "Green", "Aqua", "Blue", "Purple", "Magenta")

# name -> (min, max)
RANGES: dict[str, tuple[float, float]] = {
    "Exposure2012": (-5, 5),
    "Contrast2012": (-100, 100),
    "Highlights2012": (-100, 100),
    "Shadows2012": (-100, 100),
    "Whites2012": (-100, 100),
    "Blacks2012": (-100, 100),
    "Texture": (-100, 100),
    "Clarity2012": (-100, 100),
    "Dehaze": (-100, 100),
    "Vibrance": (-100, 100),
    "Saturation": (-100, 100),
    "Temperature": (2000, 50000),  # Kelvin, raw files
    "Tint": (-150, 150),
    "Sharpness": (0, 150),
    "LuminanceSmoothing": (0, 100),
    "ColorNoiseReduction": (0, 100),
    "PostCropVignetteAmount": (-100, 100),
    "GrainAmount": (0, 100),
}
for _color in _HSL_COLORS:
    for _kind in ("Hue", "Saturation", "Luminance"):
        RANGES[f"{_kind}Adjustment{_color}"] = (-100, 100)

BOOLEANS = {"ConvertToGrayscale"}

# Read-only context shown next to the editable values.
INFO_KEYS = {"ProcessVersion", "CameraProfile", "WhiteBalance"}

MAX_PRESET_NAME = 80


def readable_keys() -> set[str]:
    return set(RANGES) | BOOLEANS | INFO_KEYS


def validate(settings: Any) -> tuple[dict[str, Any], str | None]:
    """Return (clean settings, error message)."""
    if not isinstance(settings, dict) or not settings:
        return {}, "settings must be a non-empty object of develop parameters"
    if len(settings) > 60:
        return {}, "at most 60 parameters per call"
    clean: dict[str, Any] = {}
    for key, value in settings.items():
        if key in BOOLEANS:
            if not isinstance(value, bool):
                return {}, f"{key} must be true or false"
            clean[key] = value
        elif key in RANGES:
            lo, hi = RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return {}, f"{key} must be a number"
            if not lo <= value <= hi:
                return {}, f"{key} must be between {lo} and {hi}"
            clean[key] = value
        else:
            return {}, f"unsupported develop parameter '{key}'. Allowed: {', '.join(sorted(RANGES) + sorted(BOOLEANS))}"
    return clean, None


def valid_preset_name(name: Any) -> bool:
    return isinstance(name, str) and 0 < len(name.strip()) <= MAX_PRESET_NAME and "/" not in name
