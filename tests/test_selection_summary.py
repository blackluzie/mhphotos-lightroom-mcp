import asyncio
from copy import deepcopy
from unittest.mock import patch

import pytest
from mhphotos_lightroom_mcp.selection_summary import summarize_selection
from mhphotos_lightroom_mcp.server import mcp, selected_photo_summary

PHOTOS = [
    {'fileName':'PRIVATE_A.DNG','cameraModel':'Camera A','lens':'Lens A','fileType':'DNG','rating':5,'pickStatus':1},
    {'fileName':'PRIVATE_B.DNG','cameraModel':'Camera A','lens':'Lens B','fileType':'DNG','rating':'5','pickStatus':'-1'},
    {'fileName':'PRIVATE_C.JPG','cameraModel':'Camera B','rating':'0','pickStatus':0},
    {'fileName':'PRIVATE_D.JPG','rating':'','pickStatus':'','lens':'   '},
]


def test_summary_groups_values_missing_fields_and_keeps_input_unchanged():
    original = deepcopy(PHOTOS)
    result = summarize_selection(PHOTOS)
    assert result['photo_count'] == 4
    assert result['groups']['cameraModel']['values'] == [
        {'value':'Camera A','count':2}, {'value':'Camera B','count':1},
    ]
    assert result['groups']['rating'] == {
        'values':[{'value':'0','count':1}, {'value':'5','count':2}], 'missing_count':1,
    }
    assert result['groups']['pickStatus']['missing_count'] == 1
    assert result['groups']['cameraMake']['missing_count'] == 4
    assert result['available_metadata_counts']['lens'] == 2
    assert result['available_metadata_counts']['dateTimeOriginalISO8601'] == 0
    assert 'PRIVATE_' not in str(result)
    assert result['catalog_writes'] is False
    assert PHOTOS == original


@pytest.mark.parametrize('reason', ['no_selection','unauthorized','bridge_unavailable','bridge_not_configured'])
def test_summary_does_not_report_data_when_snapshot_unavailable(reason):
    with patch('mhphotos_lightroom_mcp.server.selected_photo_metadata', return_value={'photos':[],'reason':reason}):
        assert selected_photo_summary() == {'photo_count':0,'reason':reason}


@pytest.mark.parametrize('photos', [[], [{}], [{'GPS':'private'}], [{'rating':True}], PHOTOS*13])
def test_summary_rejects_unexpected_or_unbounded_metadata(photos):
    with patch('mhphotos_lightroom_mcp.server.selected_photo_metadata', return_value={'photos':photos}):
        assert selected_photo_summary() == {'photo_count':0,'reason':'invalid_response'}


def test_registered_mcp_summary_and_legacy_fields():
    async def run():
        with patch('mhphotos_lightroom_mcp.server.selected_photo_metadata', return_value={'photos':PHOTOS}):
            result = await mcp.call_tool('selected_photo_summary', {})
        import json
        content = result[0] if isinstance(result, tuple) else result
        data = json.loads(next(c.text for c in content if c.type == 'text'))
        assert data['photo_count'] == 4
        legacy = summarize_selection([{'fileName':'old.jpg','rating':'0','pickStatus':'0'}])
        assert legacy['groups']['cameraModel']['missing_count'] == 1
        assert legacy['groups']['rating']['values'] == [{'value':'0','count':1}]
    asyncio.run(run())
