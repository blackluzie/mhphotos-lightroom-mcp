import asyncio
import base64
import io
import json
from unittest.mock import patch

import pytest
from mcp.types import ImageContent
from mhphotos_lightroom_mcp.contact_sheet import render_contact_sheet
from mhphotos_lightroom_mcp.server import lightroom_collection_contact_sheet
from PIL import Image


def jpeg(color='red', size=(320, 200), fmt='JPEG'):
    image = Image.new('RGB', size, color)
    output = io.BytesIO()
    exif = Image.Exif()
    exif[270] = 'PRIVATE'
    image.save(output, format=fmt, exif=exif, comment=b'PRIVATE', icc_profile=b'PRIVATE')
    return base64.b64encode(output.getvalue()).decode('ascii')


def statuses(*values):
    return [{'index': i, 'status': value} for i, value in enumerate(values, 1)]


def test_sheet_keeps_missing_slot_order_and_strips_metadata():
    result = render_contact_sheet(statuses('available', 'preview_timeout', 'available'),
                                  [jpeg('red'), jpeg('blue')], 20)
    raw = base64.b64decode(result)
    assert b'PRIVATE' not in raw
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (768, 204) and image.mode == 'RGB'
        assert not image.getexif() and not any(k in image.info for k in ('icc_profile', 'comment', 'xmp'))
        red, missing, blue = [image.getpixel((x, 125)) for x in (128, 384, 640)]
        assert red[0] > 200 and red[2] < 30
        assert blue[2] > 200 and blue[0] < 30
        assert max(missing) - min(missing) < 20


def test_maximum_sheet_has_bounded_dimensions_and_payload():
    result = render_contact_sheet(statuses(*(['available'] * 10)), [jpeg()] * 10, 0)
    raw = base64.b64decode(result)
    assert len(raw) <= 512000
    with Image.open(io.BytesIO(raw)) as image:
        assert image.size == (1280, 408)


def test_empty_and_all_unavailable_pages_do_not_invent_images():
    assert render_contact_sheet([], [], 0) is None
    assert render_contact_sheet(statuses('preview_timeout', 'preview_unavailable'), [], 0) is None


@pytest.mark.parametrize('state,images,offset', [
    (statuses('available'), [], 0),
    (statuses('preview_timeout'), [jpeg()], 0),
    ([{'index': 0, 'status': 'available'}], [jpeg()], 0),
    ([{'index': 1, 'status': []}], [], 0),
    (statuses('unknown'), [], 0),
    (statuses(*(['available'] * 11)), [jpeg()] * 11, 0),
    (statuses('available'), [jpeg()], True),
    (statuses('available'), [jpeg()], -1),
])
def test_invalid_mapping_or_unbounded_input_is_rejected(state, images, offset):
    with pytest.raises(ValueError):
        render_contact_sheet(state, images, offset)


@pytest.mark.parametrize('encoded', [
    'not base64!', base64.b64encode(b'not an image').decode(),
    jpeg(size=(1025, 10)), jpeg(fmt='PNG'),
    'A' * 700001, base64.b64encode(b'x' * 512001).decode(),
], ids=['invalid-base64', 'not-an-image', 'oversize-dimensions',
        'png-not-jpeg', 'oversize-encoded', 'oversize-decoded'])
def test_non_jpeg_or_oversize_preview_is_rejected(encoded):
    with pytest.raises(ValueError):
        render_contact_sheet(statuses('available'), [encoded], 0)


def payload(previews):
    return {'photos': [{'fileName': 'TEST.DNG', 'rating': '', 'pickStatus': '0'}] * 3,
            'total_count': 30, 'offset': 20, 'has_more': True,
            'session_id': 'a' * 32, 'previews': previews}


def test_native_tool_returns_one_sheet_and_unambiguous_position_mapping():
    data = payload([{'index': 1, 'jpeg': jpeg()},
                    {'index': 2, 'error': 'preview_timeout'},
                    {'index': 3, 'jpeg': jpeg('blue')}])
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=data) as fetch:
        result = asyncio.run(lightroom_collection_contact_sheet(['Travel'], offset=20, limit=3, session_id='a' * 32))
    assert [block.type for block in result] == ['text', 'image']
    assert isinstance(result[1], ImageContent) and result[1].mimeType == 'image/jpeg'
    metadata = json.loads(result[0].text)
    assert metadata['catalog_writes'] is False and metadata['contact_sheet']['available'] is True
    labels = metadata['contact_sheet']['labels']
    assert all(len(label['metadata_fingerprint']) == 64 for label in labels)
    assert len({label['metadata_fingerprint'] for label in labels}) == 3
    assert [{k: v for k, v in label.items() if k != 'metadata_fingerprint'} for label in labels] == [
        {'photo_number': 21, 'collection_offset': 20, 'preview_status': 'available'},
        {'photo_number': 22, 'collection_offset': 21, 'preview_status': 'preview_timeout'},
        {'photo_number': 23, 'collection_offset': 22, 'preview_status': 'available'},
    ]
    fetch.assert_called_once_with('read', {
        'operation': 'collection_photos', 'path': ['Travel'], 'offset': 20,
        'limit': 3, 'previews': True, 'session_id': 'a' * 32,
    }, timeout=35)


def test_missing_previews_and_backend_errors_remain_explicit():
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=payload([
        {'index': i, 'error': 'preview_timeout'} for i in range(1, 4)])):
        result = asyncio.run(lightroom_collection_contact_sheet(['Travel'], offset=20, limit=3))
    assert len(result) == 1
    assert json.loads(result[0].text)['contact_sheet']['available'] is False
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value={'error': 'catalog_changed'}):
        result = asyncio.run(lightroom_collection_contact_sheet(['Travel']))
    assert json.loads(result[0].text)['reason'] == 'catalog_changed'
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live') as fetch:
        result = asyncio.run(lightroom_collection_contact_sheet(['Travel'], limit=11))
    fetch.assert_not_called()
    assert json.loads(result[0].text)['reason'] == 'invalid_live_request'


def test_bad_image_returns_fixed_failure_without_private_exception():
    with patch('mhphotos_lightroom_mcp.live_worker_io.post_live', return_value=payload([
        {'index': i, 'jpeg': 'PRIVATE'} for i in range(1, 4)])):
        result = asyncio.run(lightroom_collection_contact_sheet(['Travel'], offset=20, limit=3))
    assert len(result) == 1 and 'PRIVATE' not in result[0].text
    assert json.loads(result[0].text)['reason'] == 'contact_sheet_unavailable'


def test_contact_sheet_is_native_read_only_stdio_tool_not_public_http():
    from mcp.server.fastmcp import FastMCP
    from mhphotos_lightroom_mcp.server import mcp, register_http_tools

    async def inspect():
        tool = next(t for t in await mcp.list_tools() if t.name == 'lightroom_collection_contact_sheet')
        assert tool.outputSchema is None
        assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
        http = FastMCP('test')
        register_http_tools(http)
        assert 'lightroom_collection_contact_sheet' not in {t.name for t in await http.list_tools()}
    asyncio.run(inspect())
