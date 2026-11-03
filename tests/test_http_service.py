import asyncio
import json
from pathlib import Path

import httpx
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
        'get_develop_settings', 'list_presets', 'list_changes',
        'catalog_audit_start', 'catalog_audit_status', 'catalog_audit_results',
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

    marker = json.loads((Path(a['path']).parent / 'ready.json').read_text())
    assert marker['files'][0]['name'] == 'photo.jpg'
    assert marker['files'][0]['sha256'] == a['sha256']


def _webhook_setup(tmp_path, monkeypatch, handler):
    class JPEGExporter(MockAdapter):
        def export_photos(self, photo_ids, dest_dir):
            Image.new('RGB', (100, 80)).save(str(dest_dir) + '/photo.jpg')
            return {'exported': ['photo.jpg']}
    server.set_adapter(JPEGExporter())
    monkeypatch.setenv('MHPHOTOS_EXPORT_DIR', str(tmp_path / 'exports'))
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    token = tmp_path / http_service.WEBHOOK_TOKEN
    token.parent.mkdir(parents=True, mode=0o700)
    token.write_text('ab' * 32)
    token.chmod(0o600)
    real = httpx.AsyncClient
    monkeypatch.setattr(http_service.httpx, 'AsyncClient',
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_export_notifies_n8n_without_local_paths(tmp_path, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)
    _webhook_setup(tmp_path, monkeypatch, handler)
    monkeypatch.setenv('MHPHOTOS_N8N_WEBHOOK_URL', 'https://n8n.example/webhook/mhphotos-export-ready')
    result = asyncio.run(http_service.export_photos(['p1'], confirm=True))
    assert result['n8n_notified'] is True
    request = seen[0]
    assert request.headers['X-MHPhotos-Token'] == 'ab' * 32
    body = json.loads(request.content)
    assert body['event'] == 'mhphotos.export_ready'
    assert body['assets'] == [{'id': result['files'][0]['sha256'], 'filename': 'photo.jpg',
        'bytes': result['files'][0]['bytes']}]
    assert str(tmp_path) not in request.content.decode()


def test_failed_notification_keeps_export(tmp_path, monkeypatch):
    def handler(request):
        raise httpx.ConnectError('n8n unreachable')
    _webhook_setup(tmp_path, monkeypatch, handler)
    monkeypatch.setenv('MHPHOTOS_N8N_WEBHOOK_URL', 'https://n8n.example/webhook/x')
    result = asyncio.run(http_service.export_photos(['p1'], confirm=True))
    assert result['applied'] is True and result['n8n_notified'] is False
    assert (Path(result['files'][0]['path']).parent / 'ready.json').exists()


def test_without_webhook_nothing_is_sent(tmp_path, monkeypatch):
    def handler(request):
        raise AssertionError('must not be called')
    _webhook_setup(tmp_path, monkeypatch, handler)
    monkeypatch.delenv('MHPHOTOS_N8N_WEBHOOK_URL', raising=False)
    result = asyncio.run(http_service.export_photos(['p1'], confirm=True))
    assert result['applied'] is True and 'n8n_notified' not in result


def test_webhook_url_must_be_plain_https():
    import pytest
    assert http_service.webhook_url(None) is None
    for bad in ('http://n8n.example/w', 'https://u:p@n8n.example/w', 'https://n8n.example/w?x=1'):
        with pytest.raises(ValueError):
            http_service.webhook_url(bad)


def test_confirmed_export_never_waits_for_a_client_question(tmp_path, monkeypatch):
    # Stateless HTTP cannot deliver an elicitation; the export must not try (it would hang).
    class JPEGExporter(MockAdapter):
        def export_photos(self, photo_ids, dest_dir):
            Image.new('RGB', (10, 10)).save(str(dest_dir) + '/photo.jpg')
            return {'exported': ['photo.jpg']}

    async def must_not_ask(*args, **kwargs):
        raise AssertionError('http export must not elicit')
    server.set_adapter(JPEGExporter())
    monkeypatch.setattr(server.gate, 'approve', must_not_ask)
    monkeypatch.setenv('MHPHOTOS_EXPORT_DIR', str(tmp_path / 'exports'))
    monkeypatch.delenv('MHPHOTOS_N8N_WEBHOOK_URL', raising=False)
    result = asyncio.run(http_service.export_photos(['p1'], confirm=True))
    assert result['applied'] is True


def test_catalog_edits_only_when_enabled_and_never_delete_or_publish():
    app = FastMCP('test')
    http_service.register_tools(app, writes=True)
    tools = {tool.name: tool for tool in asyncio.run(app.list_tools())}
    assert set(http_service.WRITE_TOOLS) <= set(tools)
    assert tools['set_flag'].annotations.readOnlyHint is False
    assert tools['set_rating'].annotations.destructiveHint is True
    assert tools['create_preset'].annotations.destructiveHint is False
    forbidden = {'mhphotos_publish', 'send_to_mhphotos_inbox', 'mhphotos_schedule_instagram',
                 'mhphotos_cancel_instagram', 'lightroom_selftest'}
    assert not forbidden & set(tools)


def test_consent_page_names_edits_only_when_enabled(monkeypatch):
    from mhphotos_lightroom_mcp.oauth_http import consent_text
    monkeypatch.delenv('MHPHOTOS_HTTP_WRITES', raising=False)
    assert 'Keine Katalog' in consent_text()
    monkeypatch.setenv('MHPHOTOS_HTTP_WRITES', '1')
    assert 'Abgelehnt' in consent_text() and 'Keine L&ouml;schungen' in consent_text()


def test_instagram_post_id_travels_with_a_single_export(tmp_path, monkeypatch):
    class JPEGExporter(MockAdapter):
        def export_photos(self, photo_ids, dest_dir):
            Image.new('RGB', (10, 10)).save(str(dest_dir) + '/photo.jpg')
            return {'exported': ['photo.jpg']}
    server.set_adapter(JPEGExporter())
    monkeypatch.setenv('MHPHOTOS_EXPORT_DIR', str(tmp_path / 'exports'))
    monkeypatch.delenv('MHPHOTOS_N8N_WEBHOOK_URL', raising=False)
    bad = asyncio.run(http_service.export_photos(['p1', 'p2'], confirm=True, instagram_post_id='179'))
    assert bad['code'] == 'invalid_instagram_id'
    assert asyncio.run(http_service.export_photos(['p1'], instagram_post_id='../x'))['code'] == 'invalid_instagram_id'
    done = asyncio.run(http_service.export_photos(['p1'], confirm=True, instagram_post_id='17912345678901234'))
    marker = json.loads((Path(done['files'][0]['path']).parent / 'ready.json').read_text())
    assert marker['files'][0]['instagram_id'] == '17912345678901234'
    from mhphotos_lightroom_mcp.asset_delivery import completed_assets
    listed = next(iter(completed_assets(tmp_path / 'exports').values()))[1]
    assert listed['instagram_id'] == '17912345678901234'


def test_mhphotos_reads_only_with_site_configured(monkeypatch):
    monkeypatch.delenv('MHPHOTOS_INBOX_URL', raising=False)
    app = FastMCP('a')
    http_service.register_tools(app, writes=False)
    assert not set(http_service.MHPHOTOS_TOOLS) & {t.name for t in asyncio.run(app.list_tools())}
    monkeypatch.setenv('MHPHOTOS_INBOX_URL', 'https://mhphotos.example')
    app = FastMCP('b')
    http_service.register_tools(app, writes=False)
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    assert set(http_service.MHPHOTOS_TOOLS) <= set(tools)
    assert all(tools[n].annotations.readOnlyHint for n in http_service.MHPHOTOS_TOOLS)
    assert 'mhphotos_publish' not in tools


def test_mhphotos_url_is_a_plain_https_origin():
    import pytest
    assert http_service.mhphotos_url('https://mhphotos.de/') == 'https://mhphotos.de'
    for bad in ('http://mhphotos.de', 'https://mhphotos.de/admin', 'https://u:p@mhphotos.de'):
        with pytest.raises(ValueError):
            http_service.mhphotos_url(bad)
