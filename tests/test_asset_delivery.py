import asyncio
import hashlib
import json

import httpx
from mhphotos_lightroom_mcp.asset_delivery import create_app

TOKEN = 'a' * 64


def sample(root, ready=True):
    batch = root / 'export-test'
    batch.mkdir()
    photo = batch / 'photo.jpg'
    photo.write_bytes(b'\xff\xd8\xfftest-jpeg-bytes')
    digest = hashlib.sha256(photo.read_bytes()).hexdigest()
    if ready:
        (batch / 'ready.json').write_text(json.dumps({'files': [{
            'name': photo.name, 'sha256': digest, 'bytes': photo.stat().st_size,
            'width': 6000, 'height': 4000}]}))
    return photo, digest


def request(root, path, token=TOKEN):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(root, TOKEN)), base_url='http://test') as client:
            return await client.get(path, headers={'Authorization': 'Bearer ' + token})
    return asyncio.run(run())


def test_every_route_requires_token(tmp_path):
    for path in ('/healthz', '/v1/assets', '/v1/assets/' + 'a' * 64):
        assert request(tmp_path, path, token='wrong').status_code == 401


def test_in_progress_file_is_invisible(tmp_path):
    sample(tmp_path, ready=False)
    assert request(tmp_path, '/v1/assets').json() == []


def test_verified_download_matches_manifest(tmp_path):
    photo, digest = sample(tmp_path)
    listing = request(tmp_path, '/v1/assets').json()
    assert listing[0]['id'] == digest
    assert 'path' not in listing[0]
    downloaded = request(tmp_path, '/v1/assets/' + digest)
    assert downloaded.status_code == 200
    assert downloaded.content == photo.read_bytes()
    assert downloaded.headers['X-Content-SHA256'] == digest
    assert request(tmp_path, '/v1/assets/' + 'b' * 64).status_code == 404


def test_modified_file_is_no_longer_available(tmp_path):
    photo, digest = sample(tmp_path)
    photo.write_bytes(b'changed')
    assert request(tmp_path, '/v1/assets').json() == []
    assert request(tmp_path, '/v1/assets/' + digest).status_code == 404


def test_manifest_cannot_escape_export_folder(tmp_path):
    photo, _ = sample(tmp_path)
    marker = photo.parent / 'ready.json'
    data = json.loads(marker.read_text())
    data['files'][0]['name'] = '../../private.jpg'
    marker.write_text(json.dumps(data))
    assert request(tmp_path, '/v1/assets').json() == []
