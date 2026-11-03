import xml.etree.ElementTree as ET

from mhphotos_lightroom_mcp import preset_xmp, server

CRS = "{http://ns.adobe.com/camera-raw-settings/1.0/}"


def test_xmp_is_valid_and_carries_the_settings():
    text = preset_xmp.render("Münster <Abend>", {"Exposure2012": 0.35, "Highlights2012": -40,
                             "ConvertToGrayscale": False}, "A" * 32)
    desc = ET.fromstring(text).find(".//{http://www.w3.org/1999/02/22-rdf-syntax-ns#}Description")
    assert desc.get(CRS + "Exposure2012") == "+0.35"
    assert desc.get(CRS + "Highlights2012") == "-40"
    assert desc.get(CRS + "ConvertToGrayscale") == "False"
    assert desc.get(CRS + "UUID") == "A" * 32
    assert "Münster &lt;Abend&gt;" in text and "MHPhotos" in text


def test_never_overwrites(tmp_path, monkeypatch):
    monkeypatch.setenv(preset_xmp.PRESET_DIR_ENV, str(tmp_path))
    first = preset_xmp.write("Look", {"Contrast2012": 10})
    assert first["preset_file"] and (tmp_path / "Look.xmp").exists()
    again = preset_xmp.write("Look", {"Contrast2012": 99})
    assert again["preset_file"] is None
    assert 'crs:Contrast2012="+10"' in (tmp_path / "Look.xmp").read_text()


def test_file_name_is_safe():
    assert preset_xmp.file_name("../../evil") == "evil.xmp"
    assert preset_xmp.file_name("Münster Abend 2026") == "Münster Abend 2026.xmp"


import pytest


@pytest.mark.anyio
async def test_create_preset_writes_xmp(tmp_path, monkeypatch):
    monkeypatch.setenv("MHPHOTOS_ALLOW_WRITES", "1")
    monkeypatch.setenv("MHPHOTOS_CONFIRMATION", "flag")
    monkeypatch.setenv(preset_xmp.PRESET_DIR_ENV, str(tmp_path))
    done = await server.create_preset("Kreuzfahrt warm", {"Temperature": 5600, "Vibrance": 12}, confirm=True)
    assert done["applied"] and done["preset_file"].endswith("Kreuzfahrt warm.xmp")
