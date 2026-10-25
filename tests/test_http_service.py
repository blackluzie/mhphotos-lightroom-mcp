import asyncio

from mcp.server.fastmcp import FastMCP
from mhphotos_lightroom_mcp import http_service, server
from mhphotos_lightroom_mcp.adapters import MockAdapter
from PIL import Image


def test_public_tools_only_read_and_local_export():
    app = FastMCP('test')
    http_service.register_tools(app)
    tools = asyncio.run(app.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        'connection_status', 'list_collections', 'list_photos',
        'get_photo_metadata', 'get_photo_previews', 'get_contact_sheet', 'export_photos',
    }
    for tool in tools:
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.readOnlyHint is (tool.name != 'export_photos')
        assert tool.meta['securitySchemes'][0]['scopes'] == ['lightroom:read']
    export = next(tool for tool in tools if tool.name == 'export_photos')
    assert 'dest_dir' not in export.inputSchema['properties']
    assert export.inputSchema['properties']['confirm']['default'] is False


def test_export_dry_run_has_no_file_effects(tmp_path, monkeypatch):
    target = tmp_path / 'exports'
    monkeypatch.setenv('MHPHOTOS_EXPORT_DIR', str(target))
    result = asyncio.run(http_service.export_photos(['p1']))
    assert result['dry_run'] is True
    assert not target.exists()


def test_export_success_message_without_files_is_rejected(tmp_path, monkeypatch):
    class MissingExporter(MockAdapter):
        def export_photos(self, photo_ids, dest_dir):
            return {'exported': ['claimed.jpg']}
    server.set_adapter(MissingExporter())
    monkeypatch.setenv('MHPHOTOS_EXPORT_DIR', str(tmp_path / 'exports'))
    result = asyncio.run(http_service.export_photos(['p1'], confirm=True))
    assert result['code'] == 'export_incomplete'
    assert 'applied' not in result


def test_verified_export_uses_separate_batches_and_preserves_prior_files(tmp_path, monkeypatch):
    class JPEGExporter(MockAdapter):
        def export_photos(self, photo_ids, dest_dir):
            Image.new('RGB', (100, 80)).save(str(dest_dir) + '/photo.jpg')
            return {'exported': ['photo.jpg']}
    server.set_adapter(JPEGExporter())
    monkeypatch.setenv('MHPHOTOS_EXPORT_DIR', str(tmp_path / 'exports'))
    first = asyncio.run(http_service.export_photos(['p1'], confirm=True))
    second = asyncio.run(http_service.export_photos(['p1'], confirm=True))
    assert first['applied'] and second['applied']
    a, b = first['files'][0], second['files'][0]
    assert a['path'] != b['path']
    assert a['sha256'] == b['sha256']
    assert (a['width'], a['height']) == (100, 80)
