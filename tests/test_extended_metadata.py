"""Real Lua metadata projection and secure helper/HTTP/MCP integration."""

import json
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from lupa.lua51 import LuaRuntime
from mhphotos_lightroom_mcp import selection_sender
from mhphotos_lightroom_mcp.bridge import BridgeConfig, read_selection
from mhphotos_lightroom_mcp.local_bridge import make_server
from mhphotos_lightroom_mcp.pairing import ensure_bridge_token
from mhphotos_lightroom_mcp.selection_schema import (
    EXTENDED_FIELDS,
    FIELDS,
    validate_selection,
)
from mhphotos_lightroom_mcp.server import selected_photo_metadata


@pytest.fixture
def metadata():
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(
        'import = function(name) assert(name == "LrTasks"); return {pcall = pcall} end'
    )
    source = (
        Path(__file__).resolve().parents[1]
        / "lightroom/MHPhotosLightroom.lrplugin/SelectionMetadata.lua"
    )
    module = lua.execute(source.read_text())
    return lua, module


def test_lua_reads_only_allowed_fields_and_encodes_exif(metadata):
    lua, module = metadata
    photo = lua.execute("""
return {
    getFormattedMetadata = function(self, key)
        local data = {fileName='IMG_0001.DNG', cameraMake='Canon', cameraModel='EOS R5',
                      lens='RF 24-70mm', shutterSpeed='1/250 sec', aperture='f/2.8',
                      isoSpeedRating='ISO 200', focalLength='50 mm', dimensions='8192 x 5464',
                      croppedDimensions='6000 x 4000', fileType='DNG'}
        assert(data[key] ~= nil, 'unapproved formatted field')
        return data[key]
    end,
    getRawMetadata = function(self, key)
        local data = {rating=4, pickStatus=1, dateTimeOriginalISO8601='2026-10-13T10:30:00'}
        assert(data[key] ~= nil, 'unapproved raw field')
        return data[key]
    end
}
""")
    payload = json.loads(module.encode(lua.table_from([module.read(photo)])))
    assert validate_selection(payload, require_base=True)
    record = payload["photos"][0]
    assert set(record) == FIELDS
    assert record["rating"] == "4"
    assert record["dateTimeOriginalISO8601"] == "2026-10-13T10:30:00"
    assert record["shutterSpeed"] == "1/250 sec"
    assert record["dimensions"] == "8192 x 5464"


def test_lua_missing_and_failed_optional_metadata_preserves_base(metadata):
    lua, module = metadata
    photo = lua.execute("""
return {
    getFormattedMetadata = function(self,key)
        if key == 'fileName' then return 'SCAN.jpg' end
        if key == 'lens' then error('field unavailable') end
        return nil
    end,
    getRawMetadata = function(self,key)
        if key == 'pickStatus' then return 0 end
        return nil
    end
}
""")
    assert json.loads(module.encode(lua.table_from([module.read(photo)]))) == {
        "photos": [{"fileName": "SCAN.jpg", "rating": "", "pickStatus": "0"}],
    }


def test_lua_unicode_and_json_escaping_are_valid(metadata):
    lua, module = metadata
    photo = lua.execute("""
return {
    getFormattedMetadata = function(self,key)
        if key == 'fileName' then return string.rep('a',79)..'ä_END' end
        if key == 'lens' then return 'Lens "quoted" \\n line' end
        return nil
    end,
    getRawMetadata = function(self,key) return nil end
}
""")
    record = json.loads(module.encode(lua.table_from([module.read(photo)])))["photos"][
        0
    ]
    assert record["fileName"] == "a" * 79
    assert record["lens"] == 'Lens "quoted"   line'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path.resolve()
    monkeypatch.setattr(Path, "home", lambda: home)
    token = ensure_bridge_token(home / ".mhphotos-lightroom-token")
    server = make_server(token, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        selection_sender,
        "BRIDGE_URL",
        f"http://127.0.0.1:{server.server_port}/v1/selection",
    )
    monkeypatch.setenv("MHPHOTOS_BRIDGE_TOKEN", token)
    monkeypatch.setattr(
        "mhphotos_lightroom_mcp.server.BridgeConfig",
        lambda token: BridgeConfig(token=token, port=server.server_port),
    )
    try:
        yield server, token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_extended_helper_http_to_mcp_roundtrip(runtime):
    photos = [
        {
            "fileName": "SAMPLE.DNG",
            "rating": "",
            "pickStatus": "0",
            **{field: "test" for field in EXTENDED_FIELDS},
        }
    ]
    assert selection_sender.send_selection({"photos": photos}) == {
        "accepted": 1,
        "metadata_mode": "extended",
    }
    assert selected_photo_metadata() == {
        "photos": photos,
        "source": "local_bridge_snapshot",
    }


def test_old_bridge_fallback_retains_original_fields(runtime):
    server, _ = runtime
    original_get = server.RequestHandlerClass.do_GET

    def legacy_get(handler):
        if handler.path == "/v1/capabilities":
            return handler._respond(404, {"error": "not_found"})
        return original_get(handler)

    with patch.object(server.RequestHandlerClass, "do_GET", legacy_get):
        result = selection_sender.send_selection(
            {
                "photos": [
                    {
                        "fileName": "OLD.DNG",
                        "rating": "3",
                        "pickStatus": "1",
                        "cameraModel": "EOS R5",
                    }
                ]
            }
        )
    assert result == {"accepted": 1, "metadata_mode": "basic_legacy_bridge"}
    assert server.selection == [
        {"fileName": "OLD.DNG", "rating": "3", "pickStatus": "1"}
    ]


def test_capabilities_never_bypass_authentication(runtime):
    server, _ = runtime
    with pytest.raises(HTTPError) as error:
        urlopen(f"http://127.0.0.1:{server.server_port}/v1/capabilities", timeout=2)
    assert error.value.code == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "/private/original.DNG"),
        ("gps", "12,34"),
        ("cameraSerialNumber", "secret"),
        ("cameraModel", True),
        ("lens", ["a"]),
        ("dimensions", 24),
        ("lens", "x" * 81),
    ],
)
def test_rejected_metadata_preserves_previous_selection(runtime, field, value):
    server, token = runtime
    original = {"photos": [{"fileName": "KEEP.DNG", "rating": "", "pickStatus": "0"}]}
    selection_sender.send_selection(original)
    bad = {"photos": [{**original["photos"][0], field: value}]}
    with pytest.raises(ValueError):
        selection_sender.send_selection(bad)
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/selection",
        data=json.dumps(bad).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)
    assert error.value.code == 400
    assert (
        read_selection(BridgeConfig(token=token, port=server.server_port)) == original
    )


def test_encoded_byte_limit_checked_before_credentials_or_network(monkeypatch):
    payload = {
        "photos": [
            {
                "fileName": "\\" * 80,
                "rating": "",
                "pickStatus": "0",
                **{field: "\\" * 80 for field in EXTENDED_FIELDS},
            }
            for _ in range(50)
        ]
    }

    def forbidden(*args):
        raise AssertionError("Must reject payload before reading credentials")

    monkeypatch.setattr(selection_sender, "read_bridge_token", forbidden)
    with pytest.raises(ValueError, match="large"):
        selection_sender.send_selection(payload)


def test_status_tracks_selection_and_expiry_without_live_catalog_claim(runtime):
    from mhphotos_lightroom_mcp.bridge import read_status
    from mhphotos_lightroom_mcp.local_bridge import SELECTION_TTL_SECONDS
    from mhphotos_lightroom_mcp.server import connection_status

    server, token = runtime
    config = BridgeConfig(token=token, port=server.server_port)
    status = read_status(config)
    assert status["selection_state"] == "none"
    assert status["selection_count"] == 0
    assert status["selection_age_seconds"] is None
    selection_sender.send_selection(
        {
            "photos": [
                {"fileName": "STATUS.DNG", "rating": "", "pickStatus": "0"},
            ]
        }
    )
    status = connection_status()
    assert status["selection_state"] == "received"
    assert status["selection_count"] == 1
    assert 0 < status["selection_expires_in_seconds"] <= 300
    assert status["connected"] is False
    assert status["catalog_writes"] is False
    assert "selection is available" in status["message"]
    with server.state_lock:
        server.selection_updated_at -= SELECTION_TTL_SECONDS + 1
    status = connection_status()
    assert status["selection_state"] == "expired"
    assert status["selection_count"] == 0
    assert status["selection_expires_in_seconds"] == 0
    assert "expired" in status["message"]
    assert selected_photo_metadata() == {
        "photos": [],
        "reason": "no_selection",
        "source": "local_bridge_snapshot",
    }


def test_fifty_photo_selection_roundtrips_without_truncation(runtime):
    server, _ = runtime
    photos = [
        {
            "fileName": f"PHOTO_{i}.DNG",
            "rating": "",
            "pickStatus": "0",
            "cameraModel": "EOS R5",
        }
        for i in range(50)
    ]
    result = selection_sender.send_selection({"photos": photos})
    assert result["accepted"] == 50
    assert selected_photo_metadata()["photos"] == photos
    with pytest.raises(ValueError, match="invalid_photos"):
        selection_sender.send_selection({"photos": photos + [photos[0]]})
    assert server.selection == photos


def test_large_selection_on_legacy_bridge_fails_without_replacing_snapshot(runtime):
    server, _ = runtime
    base = {"fileName": "KEEP.DNG", "rating": "", "pickStatus": "0"}
    selection_sender.send_selection({"photos": [base]})
    original_get = server.RequestHandlerClass.do_GET

    def legacy_get(handler):
        if handler.path == "/v1/capabilities":
            return handler._respond(404, {"error": "not_found"})
        return original_get(handler)

    with (
        patch.object(server.RequestHandlerClass, "do_GET", legacy_get),
        pytest.raises(ValueError, match="five photos"),
    ):
        selection_sender.send_selection({"photos": [base] * 6})
    assert server.selection == [base]


@pytest.mark.parametrize(
    "response_code,response_body",
    [
        (401, {"error": "unauthorized"}),
        (500, {"error": "unavailable"}),
        (
            200,
            {"protocol_version": 2, "metadata_fields": ["fileName"], "max_photos": 50},
        ),
        (200, {"protocol_version": 2, "metadata_fields": "all", "max_photos": 50}),
    ],
)
def test_capability_errors_never_downgrade_or_replace_data(
    runtime, response_code, response_body
):
    server, _ = runtime
    base = {"fileName": "KEEP.DNG", "rating": "", "pickStatus": "0"}
    selection_sender.send_selection({"photos": [base]})
    original_get = server.RequestHandlerClass.do_GET

    def broken_capabilities(handler):
        if handler.path == "/v1/capabilities":
            return handler._respond(response_code, response_body)
        return original_get(handler)

    with (
        patch.object(server.RequestHandlerClass, "do_GET", broken_capabilities),
        pytest.raises((ValueError, HTTPError)),
    ):
        selection_sender.send_selection({"photos": [{**base, "lens": "RF 50mm"}]})
    assert server.selection == [base]
