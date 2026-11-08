"""Single-owner OAuth front end for a strict subset of the current Classic tools."""
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp.types import ToolAnnotations
from PIL import Image

from . import server
from .bridge import Bridge, BridgeAdapter
from .hot_folder import HotFolder, write_ready
from .oauth_http import SCOPE, create_http_server

READ_TOOLS = (
    'connection_status', 'list_collections', 'list_photos',
    'get_photo_metadata', 'get_photo_previews', 'get_contact_sheet',
    'get_develop_settings', 'list_presets', 'list_changes',
    'catalog_audit_start', 'catalog_audit_status', 'catalog_audit_results', 'catalog_audit_cancel',
    'photos_by_date', 'suggest_locations',
    'catalog_audit_overview', 'catalog_folder_moves', 'archive_candidates', 'presort_status', 'presort_month',
    'keyword_check_start', 'keyword_check_status', 'keyword_check_results', 'gear_usage', 'trip_outline',
    'duplicate_folders', 'missing_files_by_drive', 'gps_gaps', 'print_candidates', 'calendar_candidates',
    'weekly_report',
)
# Catalog edits, offered only when the operator enabled them (desktop.json "allow_writes").
# Nothing here deletes photos or publishes; ratings, flags, labels, titles and keywords are undoable,
# develop changes are history steps in Lightroom. "Abgelehnt" is set_flag(flag="rejected").
WRITE_TOOLS = (
    'set_rating', 'set_flag', 'set_color_label', 'set_title_caption', 'add_keywords',
    'create_collection', 'move_collection', 'create_smart_collection', 'create_keyword', 'import_photo',
    'build_smart_previews', 'set_custom_metadata', 'send_webhook', 'auto_tone', 'add_to_collection', 'create_virtual_copies',
    'virtual_copy_job', 'undo_last_change',
    'apply_preset', 'apply_develop_settings', 'sync_develop_settings', 'create_preset', 'crop_to_aspect',
    'catalog_audit_collections', 'set_location', 'rotate_photos',
    'archive_candidates_collections', 'presort_apply', 'merge_keywords', 'mark_online_in_lightroom',
    'spot_photos_collections', 'fill_gps_gaps', 'print_candidates_collections', 'calendar_collections',
)
# status of a running create_virtual_copies job: only useful with writes, reads nothing but the plug-in's memory
STATUS_TOOLS = {'virtual_copy_job'}
ADDITIVE_TOOLS = STATUS_TOOLS | {'create_collection', 'move_collection', 'create_smart_collection', 'create_keyword', 'import_photo',
    'build_smart_previews', 'add_to_collection', 'create_virtual_copies', 'create_preset', 'add_keywords',
    'catalog_audit_collections', 'archive_candidates_collections', 'presort_apply', 'mark_online_in_lightroom',
    'spot_photos_collections', 'print_candidates_collections', 'calendar_collections'}
WRITES_FLAG = 'MHPHOTOS_HTTP_WRITES'
# Read-only views of the MHPhotos app (Instagram library, inbox), offered when desktop.json
# names the MHPhotos site ("mhphotos_url"); the admin token stays in the macOS Keychain.
MHPHOTOS_TOOLS = ('mhphotos_instagram_posts', 'mhphotos_instagram_images', 'mhphotos_inbox',
    'mhphotos_instagram_queue', 'mhphotos_best_times', 'instagram_candidates', 'mhphotos_site_photos',
    'mhphotos_reel_jobs', 'spot_photos')
# Admin-only data written to the MHPhotos app (gear statistics, own photos per spot, an UNPUBLISHED blog draft):
# offered with an MHPhotos site and writes; nothing here becomes public.
MHPHOTOS_WRITE_TOOLS = ('mhphotos_sync_gear_usage', 'mhphotos_sync_spot_photos', 'mhphotos_create_blog_draft')
# Planning posts that the MHPhotos social queue later publishes on Instagram by itself. Offered only
# when desktop.json has "allow_instagram": true (and an MHPhotos site); each post is a dry run first.
INSTAGRAM_TOOLS = ('mhphotos_schedule_instagram', 'mhphotos_cancel_instagram', 'mhphotos_create_reel')
INSTAGRAM_FLAG = 'MHPHOTOS_HTTP_INSTAGRAM'
# Putting inbox items on the PUBLIC MHPhotos site and correcting photos that are already online. Offered only
# when desktop.json has "allow_publish": true (and an MHPhotos site); each change is a dry run first.
PUBLISH_TOOLS = ('mhphotos_galleries', 'mhphotos_analyze_inbox', 'mhphotos_publish', 'mhphotos_update_site_photo',
    'mhphotos_link_instagram')
PUBLISH_FLAG = 'MHPHOTOS_HTTP_PUBLISH'
# Writing a capture time into the JPEG files themselves (exiftool, with backups). Offered only when
# desktop.json has "allow_file_edits": true together with "allow_writes": true.
FILES_TOOLS = ('write_name_dates_to_files',)
FILES_FLAG = 'MHPHOTOS_HTTP_FILES'
INSTAGRAM_ID = re.compile(r'^[0-9A-Za-z_-]{1,64}$')
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


def mhphotos_url(value):
    url = urlsplit(value or '')
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('mhphotos_url must be the https origin of the MHPhotos site')
    return f'https://{url.netloc}'


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
            {'id': item['sha256'], 'filename': Path(item['path']).name, 'bytes': item['bytes'],
             **({'instagram_id': item['instagram_id']} if item.get('instagram_id') else {})} for item in files]}
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(webhook_url(url), json=payload, headers={
                'X-MHPhotos-Token': token, 'User-Agent': 'MHPhotos-Lightroom/1.0'})
        return response.status_code < 300
    except (OSError, ValueError, httpx.HTTPError):
        return False


async def export_photos(photo_ids: list[str], confirm: bool = False, instagram_post_id: str | None = None) -> dict:
    """Export up to 12 photos as local JPEGs, verifying actual files. Dry run by default.

    Destination is operator-controlled; no catalog writes, uploads or publication.
    A JPEG export is not the source RAW file. Missing files are reported as failure.
    Call once without confirm to show the plan, then with confirm=true.
    instagram_post_id (one photo only): the Instagram post this photo is the original of; it
    travels with the file into the MHPhotos inbox, which links the post when the photo is published.
    """
    if bad := server._check_ids(photo_ids):
        return bad
    if len(photo_ids) > 12:
        return {'error': 'Export at most 12 photos per request', 'code': 'invalid_batch'}
    if instagram_post_id is not None and (len(photo_ids) != 1 or not INSTAGRAM_ID.match(instagram_post_id)):
        return {'error': 'instagram_post_id needs exactly one photo and a valid post id',
                'code': 'invalid_instagram_id'}
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
        plan = {'dry_run': True, 'files': names, 'destination': dest}
        return {**plan, 'instagram_post_id': instagram_post_id} if instagram_post_id else plan
    # This stateless JSON service cannot send a server-to-client question (elicitation): such a
    # call never gets an answer and would hang. The human check is the client's own tool
    # permission (claude.ai: set export_photos to "ask"); confirm=true records the intent.
    how = 'confirm (client tool permission)'
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
    extra = {'instagram_id': instagram_post_id} if instagram_post_id else {}
    write_ready(batch, files, extra)
    result = {'applied': True, 'change_id': change_id, 'files': files, 'format': 'rendered JPEG, not original RAW'}
    notified = await notify_export_ready(batch.name, [{**item, **extra} for item in files])
    if notified is not None:
        result['n8n_notified'] = notified
    return result


def register_tools(app, writes=None):
    writes = os.environ.get(WRITES_FLAG) == '1' if writes is None else writes
    mhphotos = bool(os.environ.get('MHPHOTOS_INBOX_URL'))
    instagram = mhphotos and os.environ.get(INSTAGRAM_FLAG) == '1'
    publish = mhphotos and os.environ.get(PUBLISH_FLAG) == '1'
    files = writes and os.environ.get(FILES_FLAG) == '1'
    names = (READ_TOOLS + LOCAL_TOOLS + (WRITE_TOOLS if writes else ()) + (MHPHOTOS_TOOLS if mhphotos else ())
        + (MHPHOTOS_WRITE_TOOLS if mhphotos and writes else ())
        + (INSTAGRAM_TOOLS if instagram else ()) + (PUBLISH_TOOLS if publish else ()) + (FILES_TOOLS if files else ()))
    for name in names:
        function = export_photos if name == 'export_photos' else getattr(server, name)
        overwrites = ((name in WRITE_TOOLS and name not in ADDITIVE_TOOLS) or name == 'mhphotos_cancel_instagram'
            or name in FILES_TOOLS or name == 'mhphotos_update_site_photo')
        reads = name in READ_TOOLS or name in MHPHOTOS_TOOLS or name in STATUS_TOOLS or name == 'mhphotos_galleries'
        app.add_tool(function,
            structured_output=False if name in {'get_photo_previews', 'get_contact_sheet', 'mhphotos_instagram_images'} else None,
            annotations=ToolAnnotations(readOnlyHint=reads,
                destructiveHint=overwrites, idempotentHint=reads,
                openWorldHint=name in MHPHOTOS_TOOLS or name in INSTAGRAM_TOOLS or name in MHPHOTOS_WRITE_TOOLS
                or name in PUBLISH_TOOLS),
            meta={'securitySchemes': [{'type': 'oauth2', 'scopes': [SCOPE]}]})


def create_app():
    app = create_http_server()
    register_tools(app)
    return app.streamable_http_app()


def main():
    # Never inherit an accidental write/publish opt-in from another launch context.
    for key in ('MHPHOTOS_ALLOW_WRITES', 'MHPHOTOS_ALLOW_PUBLISH', 'MHPHOTOS_ALLOW_INSTAGRAM', WRITES_FLAG,
            'MHPHOTOS_INBOX_URL', INSTAGRAM_FLAG, FILES_FLAG, 'MHPHOTOS_ALLOW_FILE_EDITS', PUBLISH_FLAG):
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
    if config.get('mhphotos_url'):
        os.environ['MHPHOTOS_INBOX_URL'] = mhphotos_url(config['mhphotos_url'])
    if config.get('allow_instagram') is True and config.get('mhphotos_url'):
        os.environ[INSTAGRAM_FLAG] = '1'
        os.environ['MHPHOTOS_ALLOW_INSTAGRAM'] = '1'
        os.environ['MHPHOTOS_CONFIRMATION'] = 'flag'
    if config.get('allow_publish') is True and config.get('mhphotos_url'):
        os.environ[PUBLISH_FLAG] = '1'
        os.environ['MHPHOTOS_ALLOW_PUBLISH'] = '1'
        os.environ['MHPHOTOS_CONFIRMATION'] = 'flag'
    if config.get('allow_writes') is True:
        os.environ[WRITES_FLAG] = '1'
        os.environ['MHPHOTOS_ALLOW_WRITES'] = '1'
        if config.get('allow_file_edits') is True:
            os.environ[FILES_FLAG] = '1'
            os.environ['MHPHOTOS_ALLOW_FILE_EDITS'] = '1'
        # Stateless HTTP cannot ask the user (elicitation never arrives); the human check is the
        # client's per-tool permission ("ask" in claude.ai) plus the mandatory dry run / confirm=true.
        os.environ['MHPHOTOS_CONFIRMATION'] = 'flag'
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
    stop_hot_folder = start_hot_folder(config.get('hot_folder'))
    import uvicorn
    try:
        uvicorn.run(app, host='127.0.0.1', port=8766, access_log=False, proxy_headers=False,
            timeout_graceful_shutdown=10)
    finally:
        if stop_hot_folder:
            stop_hot_folder.set()
        bridge.stop()


def start_hot_folder(folder):
    """Background hot folder (desktop.json "hot_folder"); returns its stop event, or None."""
    if not folder:
        return None
    import threading

    import anyio

    def notify(batch, files):
        return anyio.run(notify_export_ready, batch, files)

    stop = threading.Event()
    watcher = HotFolder(Path(folder).expanduser(), Path(server.permissions.export_dir()), notify)
    threading.Thread(target=watcher.run, args=(stop,), name='hot-folder', daemon=True).start()
    return stop


if __name__ == '__main__':
    main()
