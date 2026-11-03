"""Single-owner OAuth front end for a strict subset of the current Classic tools."""
import hashlib
import json
import os
import stat
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import Context
from mcp.types import ToolAnnotations
from PIL import Image

from . import server
from .bridge import Bridge, BridgeAdapter
from .oauth_http import SCOPE, create_http_server

READ_TOOLS = (
    'connection_status', 'list_collections', 'list_photos',
    'get_photo_metadata', 'get_photo_previews', 'get_contact_sheet',
)
LOCAL_TOOLS = ('export_photos',)
WEBHOOK_TOKEN = '.config/mhphotos-lightroom/n8n-webhook-token'


def webhook_url(value):
    """An exact https URL without credentials, query or fragment, or None when unset."""
    if not value:
        return None
    url = urlsplit(value)
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('n8n_webhook_url must be an https URL without credentials, query or fragment')
    return value


def read_webhook_token(home: Path) -> str:
    path = home / WEBHOOK_TOKEN
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('n8n webhook token must be owner-controlled, mode 600')
        token = f.read(1024).strip()
    if len(token) != 64 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Expected a 32-byte hex n8n webhook token')
    return token


async def notify_export_ready(batch: str, files: list[dict]):
    """Tell n8n that a verified batch is ready. None = not configured; never fails the export.

    Only asset ids (SHA-256), file names and sizes are sent, never local paths.
    """
    url = os.environ.get('MHPHOTOS_N8N_WEBHOOK_URL')
    if not url:
        return None
    try:
        token = read_webhook_token(Path.home())
        payload = {'event': 'mhphotos.export_ready', 'batch': batch, 'assets': [
            {'id': item['sha256'], 'filename': Path(item['path']).name, 'bytes': item['bytes']} for item in files]}
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(webhook_url(url), json=payload, headers={
                'X-MHPhotos-Token': token, 'User-Agent': 'MHPhotos-Lightroom/1.0'})
        return response.status_code < 300
    except (OSError, ValueError, httpx.HTTPError):
        return False


async def export_photos(photo_ids: list[str], confirm: bool = False, ctx: Context | None = None) -> dict:
    """Export up to 12 photos as local JPEGs, verifying actual files. Dry run by default.

    Destination is operator-controlled; no catalog writes, uploads or publication.
    A JPEG export is not the source RAW file. Missing files are reported as failure.
    """
    if bad := server._check_ids(photo_ids):
        return bad
    if len(photo_ids) > 12:
        return {'error': 'Export at most 12 photos per request', 'code': 'invalid_batch'}
    dest = server.permissions.export_dir()
    if dest is None:
        return {'error': 'Local export is not configured', 'code': 'disabled'}
    names = []
    for pid in photo_ids:
        meta = await server._run(server._adapter.get_photo, pid)
        if server._is_error(meta):
            return meta
        names.append(meta.filename)
    if not confirm:
        return {'dry_run': True, 'files': names, 'destination': dest}
    approved, how = await server.gate.approve(ctx, f'{len(names)} Foto(s) als lokale JPEGs nach {dest} exportieren')
    if not approved:
        return server.gate.refusal(how)
    root = Path(dest).resolve()
    root.mkdir(parents=True, exist_ok=True)
    batch = root / ('export-' + uuid.uuid4().hex)
    batch.mkdir(mode=0o700)
    result = await server._run(server._adapter.export_photos, photo_ids, str(batch))
    if server._is_error(result):
        return result
    files = []
    try:
        for path in batch.iterdir():
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {'.jpg', '.jpeg'}:
                raise ValueError('Unexpected export output')
            with Image.open(path) as image:
                if image.format != 'JPEG':
                    raise ValueError('Export is not JPEG')
                pixels = image.size
                image.verify()
            files.append({'path': str(path), 'bytes': path.stat().st_size,
                'width': pixels[0], 'height': pixels[1],
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        if len(files) != len(photo_ids):
            raise ValueError('Lightroom did not create all requested JPEG files')
    except (OSError, ValueError) as error:
        return {'error': str(error), 'code': 'export_incomplete', 'destination': str(batch)}
    change_id = server.audit.record('export_photos', photo_ids, {'destination': str(batch)}, approved_by=how)
    marker = batch / '.ready.tmp'
    marker.write_text(json.dumps({'files': [
        {**item, 'name': Path(item['path']).name} for item in files
    ]}))
    marker.chmod(0o600)
    marker.replace(batch / 'ready.json')
    result = {'applied': True, 'change_id': change_id, 'files': files, 'format': 'rendered JPEG, not original RAW'}
    notified = await notify_export_ready(batch.name, files)
    if notified is not None:
        result['n8n_notified'] = notified
    return result


def register_tools(app):
    for name in READ_TOOLS + LOCAL_TOOLS:
        function = export_photos if name == 'export_photos' else getattr(server, name)
        app.add_tool(function,
            structured_output=False if name in {'get_photo_previews', 'get_contact_sheet'} else None,
            annotations=ToolAnnotations(readOnlyHint=name in READ_TOOLS,
                destructiveHint=False, idempotentHint=name in READ_TOOLS, openWorldHint=False),
            meta={'securitySchemes': [{'type': 'oauth2', 'scopes': [SCOPE]}]})


def create_app():
    app = create_http_server()
    register_tools(app)
    return app.streamable_http_app()


def main():
    # Never inherit an accidental write/publish opt-in from another launch context.
    for key in ('MHPHOTOS_ALLOW_WRITES', 'MHPHOTOS_ALLOW_PUBLISH', 'MHPHOTOS_ALLOW_INSTAGRAM'):
        os.environ.pop(key, None)
    if not server.permissions.export_dir():
        raise ValueError('An operator-controlled export directory is required')
    config_path = Path.home() / '.config/mhphotos-lightroom/desktop.json'
    config = json.loads(config_path.read_text())
    os.environ['MHPHOTOS_PUBLIC_URL'] = config['public_url']
    os.environ['MHPHOTOS_REDIRECT_URIS'] = ' '.join(config['redirect_uris'])
    missing = [key for key in ('github_client_id', 'github_owner_id') if not config.get(key)]
    if missing:
        raise ValueError(f'{config_path} lacks {", ".join(missing)}; rerun scripts/rebuild-mac.sh '
            '--github-client-id ID --github-owner-id NUMBER [--github-login LOGIN] --apply')
    os.environ['MHPHOTOS_GITHUB_CLIENT_ID'] = str(config['github_client_id'])
    os.environ['MHPHOTOS_GITHUB_OWNER_ID'] = str(config['github_owner_id'])
    os.environ['MHPHOTOS_GITHUB_LOGIN'] = config.get('github_login') or ''
    if webhook_url(config.get('n8n_webhook_url')):
        read_webhook_token(Path.home())  # Fail at start, not silently on the first export.
        os.environ['MHPHOTOS_N8N_WEBHOOK_URL'] = config['n8n_webhook_url']
    else:
        os.environ.pop('MHPHOTOS_N8N_WEBHOOK_URL', None)
    path = Path.home() / '.mhphotos-lightroom-mcp/bridge.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode):
            raise ValueError('Unsafe bridge configuration')
        bridge_config = json.loads(f.read(4096))
    if bridge_config.get('url') != 'http://127.0.0.1:47831':
        raise ValueError('Unexpected bridge URL')
    token = bridge_config.get('token')
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError('Invalid bridge token')
    bridge = Bridge(token=token, port=47831)
    server.set_adapter(BridgeAdapter(bridge))
    app = create_app()
    bridge.start()
    import uvicorn
    try:
        uvicorn.run(app, host='127.0.0.1', port=8766, access_log=False, proxy_headers=False)
    finally:
        bridge.stop()


if __name__ == '__main__':
    main()
