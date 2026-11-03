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


def _sha256(path: Path, cache: dict | None, seen: set) -> str:
    """Hash a file, reusing an earlier result while size, inode, mtime and ctime are unchanged."""
    info = path.stat()
    key = (str(path), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    seen.add(key)
    if cache is not None and key in cache:
        return cache[key]
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    value = h.hexdigest()
    if cache is not None:
        cache[key] = value
    return value


def completed_assets(root: Path, cache: dict | None = None):
    """Only explicit ready markers are eligible; in-progress JPEGs are invisible.

    With a cache, unchanged files are not read again on every request; any change to a
    file (size, inode, mtime or ctime) forces a fresh hash.
    """
    root = root.resolve()
    result = {}
    seen = set()
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
                if _sha256(path, cache, seen) != expected:
                    continue
                result[expected] = (path, {'id': expected, 'filename': name,
                    'sha256': expected, 'bytes': entry['bytes'],
                    'width': entry['width'], 'height': entry['height']})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if cache is not None:
        for key in set(cache) - seen:
            del cache[key]
    return result


def create_app(root: Path, token: str):
    hashes = {}

    def authorized(request: Request):
        return hmac.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + token).encode())

    async def status(request):
        if not authorized(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        return JSONResponse({'status': 'ok', 'mode': 'completed_exports_read_only'})

    async def listing(request):
        if not authorized(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        assets = completed_assets(root, hashes)
        return JSONResponse([item[1] for item in assets.values()], headers={'Cache-Control': 'no-store'})

    async def download(request):
        if not authorized(request):
            return JSONResponse({'error': 'unauthorized'}, status_code=401)
        asset_id = request.path_params['asset_id']
        if not re.fullmatch(r'[a-f0-9]{64}', asset_id):
            return JSONResponse({'error': 'not_found'}, status_code=404)
        item = completed_assets(root, hashes).get(asset_id)
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
