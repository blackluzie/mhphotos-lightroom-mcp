import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest
from mhphotos_lightroom_mcp.live_photo_search import (
    SEARCH_FIELDS,
    normalized_query,
    search_page,
)
from mhphotos_lightroom_mcp.local_bridge import make_server
from mhphotos_lightroom_mcp.server import lightroom_collection_photo_search

SID = 'a' * 32
REQUEST = {'operation': 'collection_photos', 'path': ['Travel'], 'offset': 0,
           'limit': 50, 'previews': False, 'session_id': None}


def page(photos, *, offset=0, total=None, session=SID):
    total = offset + len(photos) if total is None else total
    return {'photos': photos, 'total_count': total, 'offset': offset,
            'has_more': offset + len(photos) < total, 'previews': [], 'session_id': session}


def photo(**fields):
    return {'fileName': 'TEST.DNG', 'rating': '', 'pickStatus': '0', **fields}


def test_literal_unicode_search_explains_matches_preserves_order_and_duplicates():
    first = photo(title='SÜDTIROL', caption='Su\u0308dtirol Berge')
    result = search_page(REQUEST, page([first, photo(city='Bozen'), first]), ' Südtirol ')
    assert [p['collection_offset'] for p in result['photos']] == [0, 2]
    assert result['photos'][0]['matched_fields'] == ['title', 'caption']
    assert result['photos'][0]['metadata'] == first
    assert result['query'] == 'Südtirol'
    assert result['next_offset'] is None and result['scanned_count'] == 3
    assert result['catalog_writes'] is False
    assert normalized_query('Straße') == normalized_query('STRASSE')
    assert search_page(REQUEST, page([photo(title='.*')]), '.*')['match_count'] == 1


def test_only_explicitly_allowed_metadata_is_searched_no_geographic_inference():
    result = search_page(REQUEST, page([photo(city='Bozen', cameraModel='Südtirol')]), 'Südtirol')
    assert result['match_count'] == 0
    assert result['search_fields'] == list(SEARCH_FIELDS)
    assert 'cameraModel' not in result['search_fields']
    assert search_page(REQUEST, page([photo(keywordTagsForExport='Südtirol; Wandern')]), 'WANDERN')['match_count'] == 1


def test_page_counts_next_offset_and_last_page_do_not_claim_complete_search():
    result = search_page(REQUEST, page([photo()] * 50, total=70), 'missing')
    assert result['match_count'] == 0 and result['scanned_count'] == 50
    assert result['next_offset'] == 50 and result['total_count'] == 70
    second = {**REQUEST, 'offset': 50, 'session_id': SID}
    result = search_page(second, page([photo(title='match')] * 20, offset=50), 'match')
    assert result['match_count'] == 20 and result['next_offset'] is None
    assert result['photos'][0]['collection_offset'] == 50
    assert result['photos'][-1]['collection_offset'] == 69


@pytest.mark.parametrize('query', ['', '  ', None, [], 'x'*81, 'x\ny', '\ud800'])
def test_invalid_query_rejected_before_lightroom_is_contacted(query):
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live') as fetch:
        result = asyncio.run(lightroom_collection_photo_search(['Travel'], query))
    assert result['reason'] == 'invalid_live_search_request'
    fetch.assert_not_called()


@pytest.mark.parametrize('options', [
    {'collection_path': []}, {'scan_limit': 51}, {'scan_limit': True},
    {'offset': -1}, {'offset': 1}, {'session_id': 'bad'},
])
def test_invalid_bounds_and_unbound_continuation_do_not_contact_lightroom(options):
    args = {'collection_path': ['Travel'], 'query': 'match', **options}
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live') as fetch:
        result = asyncio.run(lightroom_collection_photo_search(**args))
    assert result['reason'] == 'invalid_live_search_request'
    fetch.assert_not_called()


def test_tool_reads_one_metadata_only_page_and_preserves_backend_errors():
    data = page([photo(title='Südtirol')])
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=data) as fetch:
        result = asyncio.run(lightroom_collection_photo_search(['Travel'], 'Südtirol'))
    fetch.assert_called_once_with('read', REQUEST, timeout=35)
    assert result['match_count'] == 1
    for error in ('catalog_changed', 'collection_not_found', 'sdk_error'):
        with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value={'error': error}):
            assert asyncio.run(lightroom_collection_photo_search(['Travel'], 'x'))['reason'] == error
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', side_effect=OSError('PRIVATE')):
        assert asyncio.run(lightroom_collection_photo_search(['Travel'], 'x'))['reason'] == 'live_bridge_unavailable'


def test_invalid_or_switched_catalog_response_is_rejected():
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=page([photo()], session='b'*32)):
        result = asyncio.run(lightroom_collection_photo_search(['Travel'], 'x', session_id=SID))
    assert result['reason'] == 'invalid_live_response'
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=page([photo(path='/private')])):
        result = asyncio.run(lightroom_collection_photo_search(['Travel'], 'x'))
    assert result['reason'] == 'invalid_live_response'


def test_search_is_read_only_stdio_tool_and_absent_from_public_http():
    from mcp.server.fastmcp import FastMCP
    from mhphotos_lightroom_mcp.server import mcp, register_http_tools

    async def inspect():
        tool = next(t for t in await mcp.list_tools() if t.name == 'lightroom_collection_photo_search')
        assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
        http = FastMCP('test')
        register_http_tools(http)
        assert 'lightroom_collection_photo_search' not in {t.name for t in await http.list_tools()}
    asyncio.run(inspect())


def test_tool_uses_real_authenticated_bridge_without_new_worker_operation(monkeypatch, tmp_path):
    import threading
    import time

    from mhphotos_lightroom_mcp.pairing import ensure_bridge_token
    from mhphotos_lightroom_mcp.private_files import (
        private_directory,
        write_new_private,
    )

    root = tmp_path / 'profile'
    private_directory(root)
    token = ensure_bridge_token(root / 'bridge-token')
    server = make_server(token, port=0, live_reads=True)
    write_new_private(root / 'profile.json', ('{"port":' + str(server.server_port) + '}').encode())
    monkeypatch.setenv('MHPHOTOS_PROFILE_ROOT', str(root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        server.live_reads.poll()
        with ThreadPoolExecutor() as pool:
            future = pool.submit(asyncio.run, lightroom_collection_photo_search(['Travel'], 'Südtirol'))
            job = None
            for _ in range(200):
                job = server.live_reads.poll()
                if job:
                    break
                time.sleep(.005)
            assert job and {k: v for k, v in job.items() if k != 'id'} == REQUEST
            server.live_reads.reply(job['id'], page([photo(title='Südtirol')]))
            result = future.result(timeout=3)
            assert result['match_count'] == 1 and result['catalog_writes'] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
