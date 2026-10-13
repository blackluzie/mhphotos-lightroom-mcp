"""Collection overview boundaries, actual Lua traversal and authenticated snapshots."""

import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from lupa.lua51 import LuaError, LuaRuntime
from mhphotos_lightroom_mcp import collection_sender
from mhphotos_lightroom_mcp.bridge import BridgeConfig, read_collections
from mhphotos_lightroom_mcp.collection_schema import (
    MAX_COLLECTION_BODY,
    validate_collections,
)
from mhphotos_lightroom_mcp.local_bridge import make_server
from mhphotos_lightroom_mcp.pairing import ensure_bridge_token
from mhphotos_lightroom_mcp.selection_transport import create_transfer
from mhphotos_lightroom_mcp.server import collection_overview

OVERVIEW = {
    "collections": [
        {"id": "node1", "name": "Travel", "kind": "collection_set"},
        {"id": "node2", "name": "Favorites", "kind": "collection", "parentId": "node1"},
        {
            "id": "node3",
            "name": "Favorites",
            "kind": "smart_collection",
            "parentId": "node1",
        },
    ],
    "truncated": False,
}


@pytest.fixture
def module():
    lua = LuaRuntime(unpack_returned_tuples=True)
    root = Path(__file__).resolve().parents[1]
    module = lua.execute(
        (
            root / "lightroom/MHPhotosLightroom.lrplugin/CollectionMetadata.lua"
        ).read_text()
    )
    lua.execute("""
function collection(name,smart)
    return {getName=function() return name end, isSmartCollection=function() return smart end,
            getPhotos=function() error('Photo enumeration forbidden') end,
            getSearchDescription=function() error('Rules forbidden') end}
end
function container(name,regular,sets)
    return {getName=function() return name end,
            getChildCollections=function() return regular end,
            getChildCollectionSets=function() return sets end}
end
""")
    return lua, module


def test_lua_hierarchy_and_duplicate_names_without_photos_or_rules(module):
    lua, metadata = module
    catalog = lua.execute("""
return container('not-transmitted',{}, {
    container('Travel',{collection('Favorites',false),collection('Favorites',true)},{}),
})
""")
    overview = json.loads(metadata.encode(metadata.read(catalog)))
    assert overview == OVERVIEW
    assert validate_collections(overview)
    assert "not-transmitted" not in json.dumps(overview)
    assert "[Smart]" in metadata.preview(metadata.read(catalog))


def test_lua_count_limit_and_unicode(module):
    lua, metadata = module
    catalog = lua.execute("""
local values={}
for i=1,101 do values[i]=collection(string.rep('x',79)..'ä_END',false) end
return container('catalog',values,{})
""")
    overview = json.loads(metadata.encode(metadata.read(catalog)))
    assert len(overview["collections"]) == 100
    assert overview["truncated"] is True
    assert "Showing first 15 entries; sending all 100 listed entries." in metadata.preview(metadata.read(catalog))
    assert overview["collections"][0]["name"] == "x" * 79
    validate_collections(overview)


def test_lua_depth_and_cycles_are_bounded(module):
    lua, metadata = module
    catalog = lua.execute("""
local current=container('deep',{}, {})
for i=1,10 do current=container('set',{}, {current}) end
return container('catalog',{}, {current})
""")
    overview = json.loads(metadata.encode(metadata.read(catalog)))
    assert len(overview["collections"]) == 8
    assert overview["truncated"] is True
    validate_collections(overview)
    cycle = lua.execute(
        'local s=container("cycle",{},{}); s.getChildCollectionSets=function() return {s} end; return container("catalog",{}, {s})'
    )
    overview = json.loads(metadata.encode(metadata.read(cycle)))
    assert len(overview["collections"]) == 1
    assert overview["truncated"] is True


def test_empty_catalog_and_sdk_failure(module):
    lua, metadata = module
    empty = lua.eval('container("catalog",{}, {})')
    assert json.loads(metadata.encode(metadata.read(empty))) == {
        "collections": [],
        "truncated": False,
    }
    broken = lua.eval('{getChildCollections=function() error("SDK failure") end}')
    with pytest.raises(LuaError, match="SDK failure"):
        metadata.read(broken)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    home = tmp_path.resolve()
    monkeypatch.setattr(Path, "home", lambda: home)
    token = ensure_bridge_token(home / ".mhphotos-lightroom-token")
    server = make_server(token, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setattr(
        collection_sender,
        "BRIDGE_URL",
        f"http://127.0.0.1:{server.server_port}/v1/collections",
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
        worker.join(timeout=3)


def test_helper_to_bridge_to_mcp_and_independent_expiry(runtime):
    server, token = runtime
    assert collection_overview() == {"collections": [], "reason": "no_collections"}
    assert collection_sender.send_collections(OVERVIEW) == {"accepted": 3}
    assert collection_overview() == {**OVERVIEW, "source": "local_bridge_snapshot"}
    with server.state_lock:
        server.collections_updated_at -= 301
        server.selection = [{"fileName": "KEEP.DNG"}]
        import time

        server.selection_updated_at = time.monotonic()
    assert read_collections(BridgeConfig(token=token, port=server.server_port)) == {
        "collections": [],
        "reason": "no_collections",
    }
    assert server.collections is None
    assert server.selection == [{"fileName": "KEEP.DNG"}]


def test_empty_overview_clears_collection_data_but_not_photos(runtime):
    server, _ = runtime
    collection_sender.send_collections(OVERVIEW)
    server.selection = [{"fileName": "KEEP.DNG"}]
    collection_sender.send_collections({"collections": [], "truncated": False})
    assert collection_overview() == {
        "collections": [],
        "truncated": False,
        "source": "local_bridge_snapshot",
    }
    assert server.selection == [{"fileName": "KEEP.DNG"}]


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_authentication_required(runtime, method):
    server, _ = runtime
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/collections",
        method=method,
        data=json.dumps(OVERVIEW).encode() if method == "POST" else None,
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)
    assert error.value.code == 401


@pytest.mark.parametrize(
    "records",
    [
        [{"id": "node1", "name": "Private", "kind": "collection", "path": "/secret"}],
        [{"id": "node1", "name": "Private", "kind": "collection", "photos": []}],
        [{"id": "node1", "name": "Private", "kind": "collection", "parentId": "node1"}],
        [{"id": "node1", "name": "Private", "kind": "collection", "parentId": "node2"}],
        [
            {"id": "node1", "name": "Private", "kind": "collection"},
            {"id": "node1", "name": "Other", "kind": "collection"},
        ],
        [
            {"id": "node1", "name": "Private", "kind": "collection"},
            {"id": "node2", "name": "Other", "kind": "collection", "parentId": "node1"},
        ],
        [{"id": "node1", "name": "x" * 81, "kind": "collection"}],
        [{"id": "node1", "name": "Private\nData", "kind": "collection"}],
        [{"id": "node1", "name": "Private", "kind": "published_collection"}],
        [
            {"id": f"node{i}", "name": "Extra", "kind": "collection"}
            for i in range(1, 102)
        ],
    ],
)
def test_invalid_graph_or_private_fields_preserve_previous_snapshot(runtime, records):
    server, token = runtime
    collection_sender.send_collections(OVERVIEW)
    payload = {"collections": records, "truncated": False}
    with pytest.raises(ValueError):
        collection_sender.send_collections(payload)
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/collections",
        method="POST",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)
    assert error.value.code == 400
    assert server.collections == OVERVIEW


def test_collection_byte_limit_and_origin_boundary(runtime):
    server, token = runtime
    collection_sender.send_collections(OVERVIEW)
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/collections",
        method="POST",
        data=b"x" * (MAX_COLLECTION_BODY + 1),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)
    assert error.value.code == 413
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/collections",
        method="POST",
        data=json.dumps(OVERVIEW).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": "https://untrusted.example",
        },
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)
    assert error.value.code == 403
    assert server.collections == OVERVIEW


def test_unsupported_bridge_never_receives_collection_data(runtime):
    server, _ = runtime
    original_get = server.RequestHandlerClass.do_GET

    def older(handler):
        if handler.path == "/v1/capabilities":
            return handler._respond(200, {"protocol_version": 2})
        return original_get(handler)

    with (
        patch.object(server.RequestHandlerClass, "do_GET", older),
        pytest.raises(ValueError, match="does not support"),
    ):
        collection_sender.send_collections(OVERVIEW)
    assert server.collections is None


def test_cli_removes_accepted_transfer_file(runtime, monkeypatch, capsys):
    path = create_transfer(OVERVIEW)
    monkeypatch.setattr(sys, "argv", ["send", str(path)])
    collection_sender.main()
    assert not path.exists()
    assert json.loads(capsys.readouterr().out) == {"accepted": 3}


@pytest.mark.parametrize("decision,sent", [("cancel", False), ("ok", True)])
def test_lightroom_overview_requires_explicit_send(module, decision, sent):
    lua, metadata = module
    lua.globals().overviewModule = metadata
    lua.globals().decision = decision
    lua.execute("""
local catalog=container('catalog',{collection('Review only',false)},{})
messages={}; captured=nil; confirmed=false
require=function(name)
    if name=='CollectionMetadata' then return overviewModule end
    assert(name=='CollectionTransfer')
    return {send=function(payload) captured=payload end}
end
import=function(name)
    if name=='LrApplication' then return {activeCatalog=function() return catalog end} end
    if name=='LrTasks' then return {pcall=pcall,startAsyncTask=function(task) task() end} end
    assert(name=='LrDialogs')
    return {confirm=function(title,text,yes,no) confirmed=true; return decision end,
            message=function(title,text,level) messages[#messages+1]=text end}
end
""")
    path = (
        Path(__file__).resolve().parents[1]
        / "lightroom/MHPhotosLightroom.lrplugin/CollectionOverview.lua"
    )
    lua.execute(path.read_text())
    assert lua.globals().confirmed is True
    assert (lua.globals().captured is not None) is sent
    if sent:
        assert json.loads(lua.globals().captured) == {
            "collections": [
                {"id": "node1", "name": "Review only", "kind": "collection"}
            ],
            "truncated": False,
        }


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_no_collection_mutation_routes(runtime, method):
    server, token = runtime
    collection_sender.send_collections(OVERVIEW)
    request = Request(
        f"http://127.0.0.1:{server.server_port}/v1/collections",
        method=method,
        headers={"Authorization": f"Bearer {token}"},
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=2)
    assert error.value.code == 501
    assert server.collections == OVERVIEW


def test_failed_collection_send_removes_private_file_without_secret_output(
    runtime, monkeypatch, capsys
):
    server, _ = runtime
    path = create_transfer(OVERVIEW)
    monkeypatch.setattr(sys, "argv", ["send", str(path)])

    def unavailable(handler):
        return handler._respond(404, {"error": "not_found"})

    with (
        patch.object(server.RequestHandlerClass, "do_GET", unavailable),
        pytest.raises(SystemExit) as error,
    ):
        collection_sender.main()
    assert error.value.code == 1
    assert not path.exists()
    assert capsys.readouterr().out == '{"error": "collection_transfer_failed"}\n'
    assert server.collections is None


def test_status_exposes_collection_expiry_separately_from_selection(runtime):
    from mhphotos_lightroom_mcp.server import connection_status

    server, _ = runtime
    status = connection_status()
    assert status["collection_state"] == "none"
    assert status["collection_count"] == 0
    collection_sender.send_collections(OVERVIEW)
    status = connection_status()
    assert status["collection_state"] == "received"
    assert status["collection_count"] == 3
    assert status["collection_overview_truncated"] is False
    assert status["selection_state"] == "none"
    assert 0 < status["collection_expires_in_seconds"] <= 300
    with server.state_lock:
        server.collections_updated_at -= 301
    status = connection_status()
    assert status["collection_state"] == "expired"
    assert status["collection_count"] == 0
    assert status["collection_expires_in_seconds"] == 0
    assert server.collections is None


def test_collection_tool_without_credentials_does_not_contact_bridge(monkeypatch):
    monkeypatch.delenv("MHPHOTOS_BRIDGE_TOKEN", raising=False)
    with patch("mhphotos_lightroom_mcp.server.read_collections") as fetch:
        assert collection_overview() == {
            "collections": [],
            "reason": "bridge_not_configured",
        }
    fetch.assert_not_called()


def test_server_rejects_hierarchy_beyond_eight_levels(runtime):
    server, _ = runtime
    payload = {
        "collections": [
            {
                "id": f"node{i}",
                "name": "Nested",
                "kind": "collection_set",
                **({"parentId": f"node{i - 1}"} if i > 1 else {}),
            }
            for i in range(1, 10)
        ],
        "truncated": True,
    }
    with pytest.raises(ValueError, match="depth"):
        collection_sender.send_collections(payload)
    assert server.collections is None
