"""Token-protected, read-only delivery of completed exports to a private consumer."""
import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route


def private_token(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('Token file must be owner-controlled, mode 600')
        token = f.read(1024).strip()
    if not re.fullmatch(r'[a-f0-9]{64}', token):
        raise ValueError('Expected a 32-byte hex token')
    return token


def completed_assets(root: Path):
    """Only explicit ready markers are eligible; in-progress JPEGs are invisible."""
    root = root.resolve()
    result = {}
    for marker in sorted(root.glob('export-*/ready.json')):
        if marker.is_symlink() or marker.parent.is_symlink() or marker.stat().st_size > 65536:
            continue
        try:
            document = json.loads(marker.read_text())
            for entry in document['files']:
                name, expected = entry['name'], entry['sha256']
                if Path(name).name != name or not re.fullmatch(r'[a-f0-9]{64}', expected):
                    continue
                path = marker.parent / name
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                    continue
                if path.suffix.lower() not in {'.jpg', '.jpeg'}:
                    continue
                if path.stat().st_size != entry['bytes']:
                    continue
                if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                    continue
                result[expected] = (path, {'id': expected, 'filename': name,
                    'sha256': expected, 'bytes': entry['bytes'],
                    'width': entry['width'], 'height': entry['height']})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def create_app(root: Path, token: str):
    def authorized(request: Request):
        return hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + token).encode())

    async def status(request):
        if not authorized(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        return JSONResponse({'status': 'ok', 'mode': 'completed_exports_read_only'})

    async def listing(request):
        if not authorized(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        assets = completed_assets(root)
        return JSONResponse([item[1] for item in assets.values()], headers={'Cache-Control': 'no-store'})

    async def download(request):
        if not authorized(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        asset_id = request.path_params['asset_id']
        if not re.fullmatch(r'[a-f0-9]{64}', asset_id):
            return JSONResponse({'error': 'not_found'}, status_code=404)
        item = completed_assets(root).get(asset_id)
        if item is None:
            return JSONResponse({'error': 'not_found'}, status_code=404)
        path, metadata = item
        return FileResponse(path, media_type='image/jpeg', filename=path.name,
            headers={'Cache-Control': 'no-store', 'X-Content-SHA256': metadata['sha256']})

    return Starlette(routes=[Route('/healthz', status), Route('/v1/assets', listing),
        Route('/v1/assets/{asset_id}', download)])


def main():
    import uvicorn
    root = Path(os.environ['MHPHOTOS_EXPORT_DIR'])
    token = private_token(Path.home() / '.config/mhphotos-lightroom/automation-token')
    uvicorn.run(create_app(root, token), host='127.0.0.1', port=8768, access_log=False)


if __name__ == '__main__':
    main()
