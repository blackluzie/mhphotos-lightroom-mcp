import asyncio
import base64
import io
import json
from unittest.mock import patch

import pytest
from mhphotos_lightroom_mcp.photo_reference import (
    metadata_fingerprint,
    validate_candidate_request,
)
from mhphotos_lightroom_mcp.server import (
    lightroom_collection_candidate_preview,
    lightroom_collection_contact_sheet,
)
from PIL import Image

SID = 'a' * 32
PHOTO = {'fileName': 'TEST.DNG', 'rating': '', 'pickStatus': '0', 'title': 'Mountains'}


def jpeg():
    output = io.BytesIO()
    Image.new('RGB', (30, 20), 'red').save(output, format='JPEG')
    return base64.b64encode(output.getvalue()).decode()


def result(photo=None, *, preview=True):
    return {'photos': [dict(PHOTO if photo is None else photo)],
            'total_count': 10, 'offset': 3, 'has_more': True,
            'session_id': SID, 'previews': [{'index': 1, **({'jpeg': jpeg()} if preview else {'error': 'preview_timeout'})}]}


def reference():
    return metadata_fingerprint(['Travel'], SID, 3, PHOTO)


def call(**overrides):
    args = {'collection_path': ['Travel'], 'collection_offset': 3,
            'session_id': SID, 'metadata_fingerprint': reference(), **overrides}
    return asyncio.run(lightroom_collection_candidate_preview(**args))


def test_fingerprint_is_canonical_but_bound_to_metadata_path_session_and_position():
    expected = reference()
    assert len(expected) == 64
    assert metadata_fingerprint(['Travel'], SID, 3, dict(reversed(list(PHOTO.items())))) == expected
    assert metadata_fingerprint(['Other'], SID, 3, PHOTO) != expected
    assert metadata_fingerprint(['Travel'], 'b' * 32, 3, PHOTO) != expected
    assert metadata_fingerprint(['Travel'], SID, 4, PHOTO) != expected
    assert metadata_fingerprint(['Travel'], SID, 3, {**PHOTO, 'rating': '0'}) != expected
    without_title = {k: v for k, v in PHOTO.items() if k != 'title'}
    assert metadata_fingerprint(['Travel'], SID, 3, without_title) != expected


def test_fingerprint_refuses_extra_private_fields_or_incomplete_metadata():
    for photo in ({**PHOTO, 'path': '/PRIVATE'}, {'fileName': 'TEST.DNG'}):
        with pytest.raises(ValueError):
            metadata_fingerprint(['Travel'], SID, 3, photo)


@pytest.mark.parametrize('options', [
    {'metadata_fingerprint': None}, {'metadata_fingerprint': 'bad'},
    {'metadata_fingerprint': 'G' * 64}, {'metadata_fingerprint': True},
    {'session_id': None}, {'session_id': 'bad'},
    {'collection_offset': True}, {'collection_offset': -1}, {'collection_path': []},
])
def test_bad_reference_is_rejected_without_fetching(options):
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live') as fetch:
        content = call(**options)
    fetch.assert_not_called()
    assert json.loads(content[0].text)['reason'] == 'invalid_candidate_reference'


def test_matching_reference_returns_native_image_and_explicit_metadata_match():
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=result()) as fetch:
        content = call()
    assert [c.type for c in content] == ['text', 'text', 'image']
    metadata = json.loads(content[0].text)
    assert metadata['candidate_verification'] == {
        'metadata_match': True, 'metadata_fingerprint': reference(), 'collection_offset': 3,
    }
    assert metadata['catalog_writes'] is False
    fetch.assert_called_once_with('read', validate_candidate_request(['Travel'], 3, SID, reference()), timeout=35)


@pytest.mark.parametrize('options,photo', [
    ({}, {**PHOTO, 'fileName': 'PRIVATE-OTHER.DNG'}),
    ({}, {**PHOTO, 'rating': '5'}),
    ({'collection_path': ['Other']}, PHOTO),
])
def test_mismatch_discards_both_image_and_unexpected_metadata(options, photo):
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=result(photo)):
        content = call(**options)
    assert len(content) == 1
    assert json.loads(content[0].text) == {'photos': [], 'reason': 'photo_metadata_changed'}
    assert 'PRIVATE' not in content[0].text


def test_catalog_changes_missing_photo_and_preview_timeouts_remain_explicit():
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value={'error': 'catalog_changed'}):
        assert json.loads(call()[0].text)['reason'] == 'catalog_changed'
    empty = {'photos': [], 'total_count': 3, 'offset': 3, 'has_more': False, 'session_id': SID, 'previews': []}
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=empty):
        assert json.loads(call()[0].text)['reason'] == 'photo_unavailable'
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=result(preview=False)):
        content = call()
    assert len(content) == 1
    metadata = json.loads(content[0].text)
    assert metadata['candidate_verification']['metadata_match'] is True
    assert metadata['preview_status'] == [{'index': 1, 'status': 'preview_timeout'}]


def test_contact_sheet_reference_round_trip_uses_same_existing_read_operation():
    async def run():
        sheet_result = {'photos': [dict(PHOTO)], 'total_count': 10, 'offset': 3,
                        'has_more': True, 'session_id': SID, 'previews': [{'index': 1, 'jpeg': jpeg()}]}
        with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', side_effect=[sheet_result, result()]) as fetch:
            sheet = await lightroom_collection_contact_sheet(['Travel'], offset=3, limit=1, session_id=SID)
            metadata = json.loads(sheet[0].text)
            label = metadata['contact_sheet']['labels'][0]
            candidate = await lightroom_collection_candidate_preview(
                metadata['collection_path'], label['collection_offset'], metadata['session_id'], label['metadata_fingerprint'])
        assert fetch.call_count == 2
        assert json.loads(candidate[0].text)['candidate_verification']['metadata_match'] is True
        assert candidate[-1].type == 'image'
    asyncio.run(run())


def test_reference_is_not_a_stable_identity_for_identical_metadata():
    # Same fields at the same position cannot distinguish another physical file.
    assert metadata_fingerprint(['Travel'], SID, 3, dict(PHOTO)) == reference()
    # No identity or editing guarantee is returned by the tool.
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=result()):
        verification = json.loads(call()[0].text)['candidate_verification']
    assert set(verification) == {'metadata_match', 'metadata_fingerprint', 'collection_offset'}


def test_candidate_tool_is_native_read_only_and_not_public_http():
    from mcp.server.fastmcp import FastMCP
    from mhphotos_lightroom_mcp.server import mcp, register_http_tools

    async def inspect():
        tool = next(t for t in await mcp.list_tools() if t.name == 'lightroom_collection_candidate_preview')
        assert tool.outputSchema is None
        assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
        http = FastMCP('test')
        register_http_tools(http)
        assert 'lightroom_collection_candidate_preview' not in {t.name for t in await http.list_tools()}
    asyncio.run(inspect())
