import asyncio
import json
from copy import deepcopy
from unittest.mock import patch

import pytest
from mhphotos_lightroom_mcp.selection_filter import filter_selection
from mhphotos_lightroom_mcp.server import mcp, selected_photo_filter

PHOTOS = [
    {'fileName':'Straße_A.DNG','cameraModel':'Camera A','lens':'Lens A','fileType':'RAW','rating':5,'pickStatus':1},
    {'fileName':'Straße_B.DNG','cameraModel':'Camera A','fileType':'RAW','rating':'5','pickStatus':'0'},
    {'fileName':'Other.jpg','rating':'0','pickStatus':0},
    {'fileName':'Other.jpg','rating':'','pickStatus':''},
    {'fileName':'Other.jpg'},
]


def test_literal_casefold_combined_filters_and_input_unchanged():
    before = deepcopy(PHOTOS)
    result = filter_selection(PHOTOS, {'file_name':'STRASSE','camera_model':'camera','file_type':'raw','rating':'5'})
    assert result['photo_count'] == 2 and result['snapshot_count'] == 5
    assert result['photos'] == PHOTOS[:2]
    assert filter_selection(PHOTOS, {'lens':'Lens A','pick_status':'1'})['photos'] == PHOTOS[:1]
    assert filter_selection(PHOTOS, {'file_name':'.*'})['photo_count'] == 0
    assert PHOTOS == before
    result['photos'][0]['rating'] = 'changed'
    assert PHOTOS == before


def test_empty_absent_and_zero_values_remain_distinct_and_duplicates_preserved():
    assert filter_selection(PHOTOS, {'rating':'0'})['photos'] == PHOTOS[2:3]
    assert filter_selection(PHOTOS, {'rating':''})['photos'] == PHOTOS[3:4]
    assert filter_selection(PHOTOS, {'pick_status':'0'})['photo_count'] == 2
    assert filter_selection(PHOTOS, {'file_name':'Other.jpg'})['photo_count'] == 3
    assert filter_selection(PHOTOS, {'file_type':'RAW extra'})['photo_count'] == 0


@pytest.mark.parametrize('filters', [{}, {'camera_model':' '}, {'lens':'x'*81}, {'rating':5}, {'file_name':'bad\n'}, {'pick_status':True}])
def test_invalid_requests_never_fetch_snapshot(filters):
    with patch('mhphotos_lightroom_mcp.server.selected_photo_metadata') as fetch:
        assert selected_photo_filter(**filters)['reason'] == 'invalid_filter_request'
    fetch.assert_not_called()


@pytest.mark.parametrize('reason', ['no_selection','unauthorized','bridge_unavailable','bridge_not_configured'])
def test_unavailable_snapshot_is_not_successful_empty_match(reason):
    with patch('mhphotos_lightroom_mcp.server.selected_photo_metadata',return_value={'photos':[], 'reason':reason}):
        assert selected_photo_filter(file_type='RAW')['reason'] == reason


@pytest.mark.parametrize('photos', [[], [{'path':'/private'}], PHOTOS*11])
def test_malformed_or_unbounded_data_is_rejected(photos):
    with patch('mhphotos_lightroom_mcp.server.selected_photo_metadata',return_value={'photos':photos}):
        assert selected_photo_filter(rating='0')['reason'] == 'invalid_response'


def test_registered_mcp_filter_returns_existing_metadata_only():
    async def run():
        with patch('mhphotos_lightroom_mcp.server.selected_photo_metadata',return_value={'photos':PHOTOS}):
            result = await mcp.call_tool('selected_photo_filter', {'rating':'5','file_type':'RAW'})
        content = result[0] if isinstance(result, tuple) else result
        data = json.loads(next(c.text for c in content if c.type == 'text'))
        assert data['photos'] == PHOTOS[:2]
        assert data['catalog_writes'] is False
    asyncio.run(run())
