"""Develop presets as Adobe XMP files, the format Lightroom Classic and Camera Raw read.

Presets created through the plugin SDK (addDevelopPresetForPlugin) are not listed in the Develop
panel. A file in Camera Raw's settings folder is: Lightroom Classic shows it in the preset group
"MHPhotos" after the next start. Only whitelisted, range-checked values (develop.validate) are written.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

GROUP = "MHPhotos"
PRESET_DIR_ENV = "MHPHOTOS_PRESET_DIR"


def preset_dir() -> Path | None:
    """Camera Raw's user preset folder (macOS), or None when Lightroom/Camera Raw is not installed."""
    override = os.environ.get(PRESET_DIR_ENV)
    if override:
        return Path(override)
    camera_raw = Path.home() / "Library/Application Support/Adobe/CameraRaw"
    return camera_raw / "Settings" / GROUP if camera_raw.is_dir() else None


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:+.2f}"
    number = int(value)
    return f"+{number}" if number > 0 else str(number)


def render(name: str, settings: dict[str, Any], preset_uuid: str | None = None) -> str:
    attrs = {
        "crs:PresetType": "Normal",
        "crs:Cluster": "",
        "crs:UUID": preset_uuid or uuid.uuid4().hex.upper(),
        "crs:SupportsAmount": "False",
        "crs:SupportsColor": "True",
        "crs:SupportsMonochrome": "True",
        "crs:SupportsHighDynamicRange": "True",
        "crs:SupportsNormalDynamicRange": "True",
        "crs:SupportsSceneReferred": "True",
        "crs:SupportsOutputReferred": "True",
        "crs:CameraModelRestriction": "",
        "crs:Copyright": "",
        "crs:ContactInfo": "",
        "crs:Version": "15.0",
        "crs:ProcessVersion": "11.0",
        **{f"crs:{key}": _value(value) for key, value in settings.items()},
        "crs:HasSettings": "True",
    }
    lines = "\n".join(f"   {key}={quoteattr(val)}" for key, val in attrs.items())
    alt = '<rdf:Alt><rdf:li xml:lang="x-default">{}</rdf:li></rdf:Alt>'
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0">\n'
        ' <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '  <rdf:Description rdf:about=""\n'
        '    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"\n'
        f"{lines}>\n"
        f"   <crs:Name>{alt.format(escape(name))}</crs:Name>\n"
        f"   <crs:Group>{alt.format(GROUP)}</crs:Group>\n"
        "  </rdf:Description>\n"
        " </rdf:RDF>\n"
        "</x:xmpmeta>\n"
    )


def file_name(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß _.-]+", "_", name).strip(" ._") or "Preset"
    return stem[:80] + ".xmp"


def write(name: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Write the preset file; never overwrites an existing one."""
    folder = preset_dir()
    if folder is None:
        return {"preset_file": None, "preset_file_note": "Camera Raw settings folder not found; no XMP preset written"}
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / file_name(name)
    try:
        with path.open("x", encoding="utf-8") as f:
            f.write(render(name, settings))
    except FileExistsError:
        return {"preset_file": None, "preset_file_note": f"A preset file named {path.name} already exists; not replaced"}
    return {"preset_file": str(path),
            "preset_file_note": f"Lightroom Classic shows it in the preset group '{GROUP}' after the next restart"}
