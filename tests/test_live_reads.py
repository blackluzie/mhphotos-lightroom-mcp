import asyncio
import base64
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest
from lupa.lua51 import LuaError, LuaRuntime
from mhphotos_lightroom_mcp.live_reads import (
    LiveReads,
    clean_preview,
    validate_request,
    validate_result,
)
from mhphotos_lightroom_mcp.local_bridge import make_server
from mhphotos_lightroom_mcp.server import (
    lightroom_collection_photos,
    lightroom_collections,
)
from PIL import Image

SID='a'*32
REQUEST={'operation':'collection_photos','path':['Travel','09/2026'],'offset':0,'limit':10,'previews':False,'session_id':None}
PHOTO={'fileName':'TEST.DNG','rating':'','pickStatus':'0'}
RESULT={'photos':[PHOTO],'total_count':1,'offset':0,'has_more':False,'previews':[],'session_id':SID}
OVERVIEW={'collections':[{'id':'node1','name':'Travel','kind':'collection_set'},{'id':'node2','name':'09/2026','kind':'collection','parentId':'node1'}],'truncated':False,'session_id':SID}


def jpeg():
    image=Image.new('RGB',(30,20),'red'); data=io.BytesIO()
    exif=Image.Exif(); exif[270]='PRIVATE'; exif[274]=6
    image.save(data,format='JPEG',exif=exif,comment=b'PRIVATE',icc_profile=b'PRIVATE')
    return data.getvalue()


def test_preview_is_pixel_only_rotated_and_bounded():
    encoded=clean_preview(base64.b64encode(jpeg()).decode())
    raw=base64.b64decode(encoded)
    assert b'PRIVATE' not in raw
    with Image.open(io.BytesIO(raw)) as image:
        assert not image.getexif()
        assert 'icc_profile' not in image.info and 'comment' not in image.info
        assert image.size==(20,30)


@pytest.mark.parametrize('data', [None, {}, {'operation':[]}, {'operation':'delete'}, {'operation':'collections','write':True},
    {**REQUEST,'limit':51}, {**REQUEST,'previews':True,'limit':11}, {**REQUEST,'offset':True},
    {**REQUEST,'path':[]}, {**REQUEST,'path':['bad\n']}, {**REQUEST,'path':['x'*81]}, {**REQUEST,'session_id':'bad'}])
def test_arbitrary_or_unsafe_requests_rejected(data):
    with pytest.raises(ValueError): validate_request(data)


@pytest.mark.parametrize('data', [None, {**RESULT,'photos':[{'path':'/private'}]}, {**RESULT,'total_count':2},
    {**RESULT,'has_more':True}, {**RESULT,'session_id':'bad'}, {**RESULT,'offset':True}, {**RESULT,'previews':[{}]}])
def test_invalid_results_rejected(data):
    with pytest.raises(ValueError): validate_result(REQUEST,data)


def test_invalid_images_and_empty_collections_are_explicit():
    request={**REQUEST,'previews':True}
    result={**RESULT,'previews':[{'index':1,'jpeg':base64.b64encode(b'not a JPEG').decode()}]}
    with pytest.raises(ValueError): validate_result(request,result)
    assert validate_result(request,{**RESULT,'previews':[{'index':1,'error':'preview_timeout'}]})['previews'][0]['error']=='preview_timeout'
    assert validate_result(REQUEST,{**RESULT,'photos':[],'total_count':0})['photos']==[]
    with pytest.raises(ValueError): validate_result({**REQUEST,'session_id':'b'*32},RESULT)


def test_queue_claim_reply_stop_and_late_replay():
    queue=LiveReads(enabled=True)
    assert queue.submit(REQUEST)['error']=='lightroom_worker_offline'
    assert queue.poll() is None
    with ThreadPoolExecutor() as pool:
        future=pool.submit(queue.submit,REQUEST)
        for _ in range(100):
            job=queue.poll()
            if job: break
            time.sleep(.005)
        assert job
        assert queue.poll() is None
        queue.reply(job['id'],RESULT)
        assert future.result(timeout=2)==RESULT
        with pytest.raises(ValueError): queue.reply(job['id'],RESULT)
        future=pool.submit(queue.submit,REQUEST)
        for _ in range(100):
            job=queue.poll()
            if job: break
            time.sleep(.005)
        queue.stop()
        assert future.result(timeout=2)['error']=='worker_stopped'
        assert not queue.online() and not queue.jobs
        with pytest.raises(ValueError): queue.reply(job['id'],RESULT)


def test_queue_busy_timeout_and_disabled(monkeypatch):
    assert LiveReads().submit(REQUEST)['error']=='live_reads_disabled'
    monkeypatch.setattr('mhphotos_lightroom_mcp.live_reads.JOB_TTL',.2)
    queue=LiveReads(True); queue.poll()
    with ThreadPoolExecutor() as pool:
        futures=[pool.submit(queue.submit,REQUEST) for _ in range(2)]
        for _ in range(100):
            with queue.lock: count=len(queue.jobs)
            if count==2: break
            time.sleep(.001)
        assert queue.submit(REQUEST)['error']=='live_busy'
        assert all(f.result(timeout=2)['error']=='live_timeout' for f in futures)
        assert not queue.jobs


@pytest.fixture
def live_server():
    server=make_server('test-token-'+'x'*40,port=0,live_reads=True)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    yield server
    server.shutdown();server.server_close();worker.join(2)


def post(server,route,data,token=True,origin=None):
    headers={'Content-Type':'application/json'}
    if token: headers['Authorization']='Bearer '+server.bridge_token
    if origin: headers['Origin']=origin
    request=Request(f'http://127.0.0.1:{server.server_port}/v1/live/{route}',data=json.dumps(data).encode(),headers=headers)
    with build_opener(ProxyHandler({})).open(request,timeout=3) as response: return json.load(response)


def test_http_live_roundtrip_never_overwrites_manual_snapshots(live_server):
    server=live_server
    server.selection=[PHOTO]; server.selection_updated_at=time.monotonic()
    assert post(server,'poll',{})=={'request':None}
    with ThreadPoolExecutor() as pool:
        result=pool.submit(post,server,'read',REQUEST)
        for _ in range(100):
            job=post(server,'poll',{})['request']
            if job: break
            time.sleep(.005)
        assert job
        assert post(server,'reply',{'id':job['id'],'result':RESULT})=={'accepted':True}
        assert result.result(timeout=2)==RESULT
    assert server.selection==[PHOTO] and server.collections is None
    assert post(server,'stop',{})=={'stopped':True}
    assert post(server,'read',REQUEST)['error']=='lightroom_worker_offline'


@pytest.mark.parametrize('route,data', [('poll',{}),('stop',{}),('read',REQUEST),('reply',{'id':SID,'result':RESULT})])
def test_http_auth_and_origin_boundary(live_server,route,data):
    with pytest.raises(HTTPError) as error: post(live_server,route,data,token=False)
    assert error.value.code==401
    with pytest.raises(HTTPError) as error: post(live_server,route,data,origin='https://evil.example')
    assert error.value.code==403


def test_default_bridge_does_not_accept_live_reads():
    server=make_server('test-token-'+'x'*40,port=0)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        with pytest.raises(HTTPError) as error: post(server,'poll',{})
        assert error.value.code==403
    finally: server.shutdown();server.server_close();worker.join(2)


def test_mcp_live_reads_and_native_images():
    async def run():
        with patch('mhphotos_lightroom_mcp.live_worker_io.post_live',return_value=OVERVIEW):
            result=await lightroom_collections('09/2026')
        assert result['collections'][0]['collection_path']==['Travel','09/2026']
        assert result['catalog_writes'] is False
        result={**RESULT,'previews':[{'index':1,'jpeg':clean_preview(base64.b64encode(jpeg()).decode())}]}
        with patch('mhphotos_lightroom_mcp.live_worker_io.post_live',return_value=result):
            content=await lightroom_collection_photos(['Travel','09/2026'])
        assert [c.type for c in content]==['text','text','image']
        assert json.loads(content[0].text)['catalog_writes'] is False
        assert content[2].mimeType=='image/jpeg'
        with patch('mhphotos_lightroom_mcp.live_worker_io.post_live') as fetch:
            content=await lightroom_collection_photos(['bad'],limit=11)
        assert json.loads(content[0].text)['reason']=='invalid_live_request'
        fetch.assert_not_called()
    asyncio.run(run())


@pytest.fixture
def lua_live():
    lua=LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
import=function(name) assert(name=='LrTasks'); return {pcall=pcall,sleep=function() end} end
function photo(name)
 return {getFormattedMetadata=function(self,key) if key=='fileName' then return name end end,
 getRawMetadata=function(self,key) if key=='pickStatus' then return 0 end end,
 requestJpegThumbnail=function(self,w,h,callback) callback('abc'); return {} end}
end
function collection(name,photos)
 return {getName=function() return name end,isSmartCollection=function() return false end,getPhotos=function() return photos end}
end
function container(name,collections,sets)
 return {getName=function() return name end,getChildCollections=function() return collections end,getChildCollectionSets=function() return sets end}
end
""")
    root=Path(__file__).resolve().parents[1]/'lightroom/MHPhotosLightroom.lrplugin'
    lua.globals().metadata=lua.execute((root/'SelectionMetadata.lua').read_text())
    lua.globals().collections=lua.execute((root/'CollectionMetadata.lua').read_text())
    lua.execute("require=function(name) if name=='SelectionMetadata' then return metadata end; assert(name=='CollectionMetadata'); return collections end")
    lua.globals().context=lua.execute((root/'LiveMetadata.lua').read_text())
    lua.execute("require=function(name) if name=='LiveMetadata' then return context end; assert(name=='CollectionMetadata'); return collections end")
    live=lua.execute((root/'LiveRead.lua').read_text())
    return lua,live


def command(operation='collection_photos',path=('Travel','09/2026'),offset=0,limit=10,previews=False,session=''):
    return '\n'.join([SID,operation,str(offset),str(limit),'1' if previews else '0',session,*[p.encode().hex() for p in path]])+'\n'


def test_actual_lua_lookup_metadata_preview_base64_and_catalog_switch(lua_live):
    lua,live=lua_live
    catalog=lua.execute("return container('catalog',{}, {container('Travel',{collection('09/2026',{photo('one'),photo('two')})},{})})")
    request=live.parse(command(limit=1,previews=True))
    result=json.loads(live.execute(catalog,request))
    assert result['total_count']==2 and result['has_more'] is True
    assert result['photos'][0]['fileName']=='one'
    assert result['previews']==[{'index':1,'jpeg':'YWJj'}]
    assert live.base64('a')=='YQ==' and live.base64('ab')=='YWI='
    request=live.parse(command(offset=1,limit=1,session=SID))
    result=json.loads(live.execute(catalog,request))
    assert result['photos'][0]['fileName']=='two'
    other=lua.execute("return container('other',{}, {})")
    lua.globals().other=other
    # A different request ID establishes a different catalog session.
    request['id']='b'*32
    assert json.loads(live.execute(other,request))['error']=='catalog_changed'


def test_actual_lua_ambiguity_not_found_and_non_executable_input(lua_live):
    lua,live=lua_live
    catalog=lua.execute("return container('catalog',{collection('duplicate',{}),collection('duplicate',{})},{})")
    assert json.loads(live.execute(catalog,live.parse(command(path=('duplicate',)))))['error']=='collection_ambiguous'
    assert json.loads(live.execute(catalog,live.parse(command(path=('missing',)))))['error']=='collection_not_found'
    with pytest.raises(LuaError): live.parse(command(operation='applyPreset'))
    with pytest.raises(LuaError): live.parse(command()+'code')
    with pytest.raises(LuaError): live.parse(command(path=('bad\n',)))


def test_actual_lua_search_context_respects_location_privacy_and_legacy_projection(lua_live):
    lua,_live=lua_live
    lua.execute("""
privatePhoto=photo('PRIVATE.DNG')
privatePhoto.getFormattedMetadata=function(self,key)
 if key=='fileName' then return 'PRIVATE.DNG' end
 if key=='keywordTagsForExport' then return 'Südtirol, Wandern' end
 if key=='title' then return 'Berge' end
 if key=='location' or key=='city' then return 'PRIVATE LOCATION' end
 if key=='gps' then error('GPS must never be read') end
end
privatePhoto.getRawMetadata=function(self,key)
 if key=='locationIsPrivate' then return true end
 if key=='pickStatus' then return 0 end
end
""")
    record=lua.globals().context.read(lua.globals().privatePhoto)
    encoded=json.loads(lua.globals().context.encode(lua.table_from([record])))['photos'][0]
    assert encoded['keywordTagsForExport']=='Südtirol, Wandern'
    assert 'PRIVATE LOCATION' not in json.dumps(encoded)
    assert 'location' not in encoded
    # Live search context does not expand the old manual transfer projection.
    old=json.loads(lua.globals().metadata.encode(lua.table_from([record])))['photos'][0]
    assert 'keywordTagsForExport' not in old
    lua.execute("privatePhoto.getRawMetadata=function(self,key) if key=='locationIsPrivate' then return false end; if key=='pickStatus' then return 0 end end")
    record=lua.globals().context.read(lua.globals().privatePhoto)
    encoded=json.loads(lua.globals().context.encode(lua.table_from([record])))['photos'][0]
    assert encoded['location']=='PRIVATE LOCATION'
    assert validate_result(REQUEST,{**RESULT,'photos':[encoded]})['photos'][0]['keywordTagsForExport']=='Südtirol, Wandern'


def test_worker_enable_cancel_does_not_poll_or_read_catalog():
    lua=LuaRuntime()
    lua.execute("""
WIN_ENV=false; executed=false; readCatalog=false; started=false
require=function(name)
 if name=='LiveRead' then return {} end
 assert(name=='Installation'); return {profileRoot='/private/profile',python='/venv/bin/python'}
end
import=function(name)
 if name=='LrTasks' then return {startAsyncTask=function() started=true end,execute=function() executed=true end} end
 if name=='LrFileUtils' then return {exists=function() return true end} end
 if name=='LrPathUtils' then return {child=function(a,b) return a..'/'..b end} end
 if name=='LrDialogs' then return {confirm=function() return 'cancel' end,message=function() end} end
 assert(name=='LrApplication'); return {activeCatalog=function() readCatalog=true end}
end
""")
    root=Path(__file__).resolve().parents[1]/'lightroom/MHPhotosLightroom.lrplugin'
    worker=lua.execute((root/'LiveWorker.lua').read_text())
    worker.start()
    assert not worker.running
    assert not lua.globals().started and not lua.globals().executed and not lua.globals().readCatalog


def test_lua_preview_timeout_and_bounded_pending_handles(lua_live):
    lua,live=lua_live
    catalog=lua.execute("""
local p=photo('late')
p.requestJpegThumbnail=function() return {} end
return container('catalog',{collection('late',{p})},{})
""")
    request=live.parse(command(path=('late',),limit=1,previews=True))
    for _ in range(10):
        result=json.loads(live.execute(catalog,request))
        assert result['previews'][0]['error']=='preview_timeout'
    result=json.loads(live.execute(catalog,request))
    assert result['previews'][0]['error']=='preview_unavailable'


def test_registered_mcp_images_are_native_content_not_base64_json():
    from mhphotos_lightroom_mcp.server import mcp
    async def run():
        tools=await mcp.list_tools()
        assert next(t for t in tools if t.name=='lightroom_collection_photos').outputSchema is None
        result={**RESULT,'previews':[{'index':1,'jpeg':clean_preview(base64.b64encode(jpeg()).decode())}]}
        with patch('mhphotos_lightroom_mcp.live_worker_io.post_live',return_value=result):
            content=await mcp.call_tool('lightroom_collection_photos',{'collection_path':['Travel','09/2026']})
        assert [c.type for c in content]==['text','text','image']
        assert 'jpeg' not in content[0].text
    asyncio.run(run())


def test_worker_files_are_private_non_executable_and_removed(live_server,tmp_path,monkeypatch):
    import os
    if os.name == 'nt':
        monkeypatch.setattr('mhphotos_lightroom_mcp.live_reads.JOB_TTL',120)
    from mhphotos_lightroom_mcp.live_worker_io import poll, reply
    from mhphotos_lightroom_mcp.pairing import ensure_bridge_token
    from mhphotos_lightroom_mcp.private_files import (
        private_directory,
        validate_private,
        write_new_private,
    )
    root=tmp_path.resolve()/'profile';private_directory(root)
    write_new_private(root/'profile.json',json.dumps({'port':live_server.server_port}).encode())
    token=ensure_bridge_token(root/'bridge-token');live_server.bridge_token=token
    private_directory(root/'transfers')
    monkeypatch.setenv('MHPHOTOS_PROFILE_ROOT',str(root))
    filename=root/'transfers'/'live-1-123456.txt'
    poll(filename);validate_private(filename)
    assert filename.read_text()==''
    filename.unlink()
    with ThreadPoolExecutor() as pool:
        future=pool.submit(live_server.live_reads.submit,REQUEST)
        for _ in range(100):
            poll(filename)
            content=filename.read_text();filename.unlink()
            if content: break
            time.sleep(.005)
        assert 'Travel' not in content and REQUEST['path'][0].encode().hex() in content
        identifier=content.splitlines()[0]
        write_new_private(filename,json.dumps({'id':identifier,'result':RESULT}).encode())
        reply(filename)
        assert future.result(timeout=2)==RESULT and not filename.exists()
    outside=tmp_path.resolve()/'live-1-123456.txt'
    with pytest.raises(ValueError): poll(outside)
    write_new_private(filename,b'KEEP')
    with pytest.raises(ValueError): poll(filename)
    assert filename.read_bytes()==b'KEEP'


def test_worker_native_helpers_to_bridge_and_jpeg_reply(live_server,tmp_path,monkeypatch,lua_live):
    import os
    import subprocess
    import sys
    if os.name=='nt': pytest.skip('Mac worker uses POSIX shell; Windows HTTP/file boundaries covered separately')
    from mhphotos_lightroom_mcp.pairing import ensure_bridge_token
    from mhphotos_lightroom_mcp.private_files import (
        private_directory,
        write_new_private,
    )
    root=tmp_path.resolve()/"private ' ü profile"; private_directory(root)
    write_new_private(root/'profile.json',json.dumps({'port':live_server.server_port}).encode())
    live_server.bridge_token=ensure_bridge_token(root/'bridge-token')
    private_directory(root/'transfers')
    monkeypatch.setenv('MHPHOTOS_PROFILE_ROOT',str(root))
    lua,live=lua_live
    lua.globals().jpegHex=jpeg().hex()
    catalog=lua.execute("""
local p=photo('TEST.DNG')
p.requestJpegThumbnail=function(self,w,h,callback)
 callback((jpegHex:gsub('..',function(pair) return string.char(tonumber(pair,16)) end)))
 return {}
end
return container('catalog',{collection('09/2026',{p})},{})
""")
    lua.globals().liveModule=live
    lua.globals().activeCatalog=catalog
    lua.globals().python=sys.executable
    lua.globals().profileRoot=str(root)
    def execute(command):
        completed=subprocess.run(command,shell=True,capture_output=True,text=True,timeout=15,check=False)
        assert live_server.bridge_token not in completed.stdout+completed.stderr
        return completed.returncode
    lua.globals().pythonExecute=execute
    lua.globals().exists=lambda path: Path(path).exists()
    lua.globals().delete=lambda path: Path(path).unlink(missing_ok=True)
    lua.execute("""
WIN_ENV=false
require=function(name) if name=='LiveRead' then return liveModule end; assert(name=='Installation'); return {python=python,profileRoot=profileRoot} end
import=function(name)
 if name=='LrTasks' then return {pcall=pcall,execute=pythonExecute,
   startAsyncTask=function(task) savedTask=task end,
   sleep=function(seconds) if seconds==2 then worker.running=false end end} end
 if name=='LrFileUtils' then return {exists=exists,delete=delete} end
 if name=='LrPathUtils' then return {child=function(a,b) return a..'/'..b end} end
 if name=='LrDialogs' then return {confirm=function() return 'ok' end,message=function() end} end
 assert(name=='LrApplication'); return {activeCatalog=function() return activeCatalog end}
end
""")
    plugin=Path(__file__).resolve().parents[1]/'lightroom/MHPhotosLightroom.lrplugin'
    lua.globals().worker=lua.execute((plugin/'LiveWorker.lua').read_text())
    lua.globals().worker.start()
    assert lua.globals().worker.running
    post(live_server,'poll',{})
    with ThreadPoolExecutor() as pool:
        future=pool.submit(post,live_server,'read',{**REQUEST,'path':['09/2026'],'previews':True})
        for _ in range(100):
            with live_server.live_reads.lock: count=len(live_server.live_reads.jobs)
            if count: break
            time.sleep(.005)
        lua.globals().savedTask()
        result=future.result(timeout=3)
    assert result['photos'][0]['fileName']=='TEST.DNG'
    assert len(result['previews'])==1
    assert b'PRIVATE' not in base64.b64decode(result['previews'][0]['jpeg'])
    assert not list((root/'transfers').glob('live-*'))


def test_worker_status_is_explicit_and_catalog_writes_remain_disabled(monkeypatch):
    from mhphotos_lightroom_mcp.server import connection_status
    monkeypatch.setenv('MHPHOTOS_BRIDGE_TOKEN','test')
    with patch('mhphotos_lightroom_mcp.server.read_status',return_value={'bridge_running':True,'lightroom_worker_online':True,'live_reads_enabled':True}):
        result=connection_status()
    assert result['connected'] is True and result['mode']=='read_only_requests'
    assert result['catalog_writes'] is False


def test_poll_is_online_in_same_coarse_clock_tick(monkeypatch):
    # Windows monotonic clocks can return the same value to adjacent threads.
    monkeypatch.setattr('mhphotos_lightroom_mcp.live_reads.time.monotonic',lambda:100.0)
    queue=LiveReads(True)
    assert not queue.online()
    assert queue.poll() is None
    assert queue.online()
    queue.stop()
    assert not queue.online()


@pytest.mark.parametrize('failure', ['poll_helper', 'request_parse', 'reply_helper'])
def test_worker_failure_stops_and_reports_only_fixed_stage(failure):
    lua=LuaRuntime()
    lua.globals().failure=failure
    lua.globals().request_text=command(operation='collections',path=(),limit=0)
    lua.execute("""
WIN_ENV=false; mode=nil; reported=nil
require=function(name)
 if name=='LiveRead' then return {
  parse=function(text) if failure=='request_parse' then error('PRIVATE') end; return {id=string.rep('a',32)} end,
  execute=function() return '{"collections":[],"truncated":false}' end} end
 return {python='/venv/python',profileRoot='/private/profile'}
end
io.open=function(path,mode) return {read=function() return request_text end,close=function() end,write=function() return true end} end
import=function(name)
 if name=='LrTasks' then return {pcall=pcall,
  execute=function(command)
   if command:find(' poll ',1,true) and failure=='poll_helper' then return 1 end
   if command:find(' reply ',1,true) and failure=='reply_helper' then return 1 end
   return 0
  end,
  startAsyncTask=function(task) savedTask=task end,sleep=function() end} end
 if name=='LrFileUtils' then return {exists=function(path) return path:sub(-10)=='/transfers' end,delete=function() end} end
 if name=='LrPathUtils' then return {child=function(a,b) return a..'/'..b end} end
 if name=='LrDialogs' then return {confirm=function() return 'ok' end,
  message=function(title,text,kind) if kind=='warning' then reported=text end end} end
 return {activeCatalog=function() return {} end}
end
""")
    plugin=Path(__file__).resolve().parents[1]/'lightroom/MHPhotosLightroom.lrplugin'
    worker=lua.execute((plugin/'LiveWorker.lua').read_text())
    worker.start()
    lua.globals().savedTask()
    assert not worker.running
    assert lua.globals().reported==f'Read-only connection stopped at {failure}. Please report this code. No catalog writes.'
