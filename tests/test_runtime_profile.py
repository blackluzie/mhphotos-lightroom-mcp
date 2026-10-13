"""Profiles cannot address the production bridge or read production transfers."""

import json
from pathlib import Path

import pytest
from mhphotos_lightroom_mcp.pairing import ensure_bridge_token
from mhphotos_lightroom_mcp.private_files import private_directory, write_new_private
from mhphotos_lightroom_mcp.runtime_profile import (
    bridge_port,
    bridge_token_path,
    bridge_url,
    transfer_path,
)
from mhphotos_lightroom_mcp.selection_transport import create_transfer, read_transfer


@pytest.fixture
def profile(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "private-profile"
    private_directory(root)
    write_new_private(root / "profile.json", b'{"port":8767}')
    ensure_bridge_token(root / "bridge-token")
    monkeypatch.setenv("MHPHOTOS_PROFILE_ROOT", str(root))
    return root


def test_default_runtime_is_unchanged(monkeypatch):
    monkeypatch.delenv("MHPHOTOS_PROFILE_ROOT", raising=False)
    assert bridge_port() == 8765
    assert bridge_token_path() == Path.home() / ".mhphotos-lightroom-token"
    assert transfer_path() == Path.home() / ".mhphotos-lightroom/transfers"


def test_profile_paths_and_transfer_are_separate(profile, tmp_path):
    assert bridge_port() == 8767
    assert bridge_url("selection") == "http://127.0.0.1:8767/v1/selection"
    assert bridge_token_path() == profile / "bridge-token"
    path = create_transfer({"photos": [{"fileName": "TEST.DNG"}]})
    assert path.parent == profile / "transfers"
    assert read_transfer(path) == {"photos": [{"fileName": "TEST.DNG"}]}
    with pytest.raises(ValueError, match="path"):
        read_transfer(tmp_path / "selection-outside.json")


@pytest.mark.parametrize("port", [8765, True, "8767", 0, 1023, 65536])
def test_unsafe_or_production_ports_refused(profile, port):
    (profile / "profile.json").write_text(json.dumps({"port": port}))
    with pytest.raises(ValueError):
        bridge_port()


def test_profile_cannot_supply_arbitrary_host(profile):
    (profile / "profile.json").write_text('{"port":8767,"host":"0.0.0.0"}')
    with pytest.raises(ValueError):
        bridge_port()


def test_relative_profile_refused(monkeypatch):
    monkeypatch.setenv("MHPHOTOS_PROFILE_ROOT", "relative")
    with pytest.raises(ValueError, match="absolute"):
        bridge_port()


def test_lua_selection_uses_profile_directory_and_runner():
    from lupa.lua51 import LuaRuntime
    lua = LuaRuntime()
    lua.execute('''
_PLUGIN={path='/plugin'}; WIN_ENV=false; writtenPath=nil; executed=nil
require=function(name)
    if name=='Installation' then return {python='/beta/bin/python',profileRoot='/private/profile'} end
    assert(name=='SelectionMetadata')
    return {read=function(photo) return {} end,encode=function(photos) return '{"photos":[]}' end}
end
import=function(name)
    if name=='LrPathUtils' then return {
        child=function(parent,child) return parent..'/'..child end,
        parent=function(path) return '/home/user' end,
        getStandardFilePath=function(key) return key=='home' and '/home/user' or '/home/user/Documents' end} end
    if name=='LrFileUtils' then return {exists=function(path)
        return path=='/plugin/Installation.lua' or path=='/private/profile/transfers'
    end,delete=function(path) end} end
    if name=='LrTasks' then return {startAsyncTask=function(task) task() end,
        execute=function(command) executed=command; return 0 end} end
    if name=='LrApplication' then return {activeCatalog=function() return {
        getTargetPhotos=function() return {{}} end,
        withReadAccessDo=function(self,task) task() end} end} end
    if name=='LrLogger' then return function() return {enable=function() end, info=function() end} end end
    assert(name=='LrDialogs'); return {message=function() end}
end
io.open=function(path,mode) writtenPath=path; return {write=function() return true end,close=function() end} end
''')
    source = Path(__file__).resolve().parents[1] / 'lightroom/MHPhotosLightroom.lrplugin/SendSelection.lua'
    lua.execute(source.read_text())
    assert lua.globals().writtenPath.startswith('/private/profile/transfers/')
    assert 'mhphotos_lightroom_mcp.profile_runner' in lua.globals().executed
    assert "'/private/profile' send" in lua.globals().executed
    assert '.local/bin/mhphotos-lightroom-send' not in lua.globals().executed
