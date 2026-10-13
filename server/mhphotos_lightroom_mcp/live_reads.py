"""Explicit, bounded read-only jobs; never interpret arbitrary catalog commands."""
import base64
import copy
import io
import re
import threading
import time
import uuid
from dataclasses import dataclass, field

from mhphotos_lightroom_mcp.collection_schema import validate_collections
from mhphotos_lightroom_mcp.selection_schema import FIELDS, validate_selection

CONTEXT_FIELDS = frozenset({'keywordTagsForExport','title','caption','location','city','stateProvince','country'})
MAX_LIVE_BODY = 8 * 1024 * 1024
JOB_TTL = 30
ERRORS = {'sdk_error', 'collection_not_found', 'collection_ambiguous', 'unsupported_collection', 'worker_stopped', 'catalog_changed'}


def validate_request(data):
    if not isinstance(data, dict) or not isinstance(data.get('operation'), str) or data['operation'] not in {'collections', 'collection_photos'}:
        raise ValueError('invalid_live_request')
    if data['operation'] == 'collections':
        if set(data) != {'operation'}:
            raise ValueError('invalid_live_request')
    else:
        if set(data) != {'operation', 'path', 'offset', 'limit', 'previews', 'session_id'}:
            raise ValueError('invalid_live_request')
        session = data['session_id']
        if session is not None and (not isinstance(session, str) or not re.fullmatch('[0-9a-f]{32}', session)):
            raise ValueError('invalid_live_request')
        path = data['path']
        if (not isinstance(path, list) or not 1 <= len(path) <= 8
            or any(not isinstance(s, str) or not 1 <= len(s.encode('utf-8')) <= 80
                   or any(ord(c) < 32 or ord(c) == 127 for c in s) for s in path)
            or type(data['offset']) is not int or not 0 <= data['offset'] <= 1000000
            or type(data['previews']) is not bool
            or type(data['limit']) is not int or not 1 <= data['limit'] <= (10 if data['previews'] else 50)):
            raise ValueError('invalid_live_request')
    return copy.deepcopy(data)


def clean_preview(encoded):
    from PIL import Image, ImageOps

    if not isinstance(encoded, str) or len(encoded) > 700000:
        raise ValueError('invalid_preview')
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > 512000:
        raise ValueError('invalid_preview')
    try:
        opened = Image.open(io.BytesIO(raw), formats=['JPEG'])
    except Image.DecompressionBombError as exc:
        raise ValueError('invalid_preview') from exc
    with opened as image:
        if image.width > 4096 or image.height > 4096:
            raise ValueError('invalid_preview')
        image = ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((1024, 1024))
        # A fresh pixel-only object carries no EXIF, XMP, comments or ICC data.
        clean = Image.new('RGB', image.size)
        clean.paste(image)
        output = io.BytesIO()
        clean.save(output, format='JPEG', quality=75, exif=b'', icc_profile=None)
    if output.tell() > 512000:
        raise ValueError('invalid_preview')
    return base64.b64encode(output.getvalue()).decode('ascii')


def validate_result(request, result):
    if not isinstance(result, dict):
        raise ValueError('invalid_live_result')  # noqa: TRY004
    if set(result) == {'error'} and isinstance(result['error'], str) and result['error'] in ERRORS:
        return result.copy()
    session = result.get('session_id')
    if not isinstance(session, str) or not re.fullmatch('[0-9a-f]{32}', session):
        raise ValueError('invalid_live_result')
    if request.get('session_id') is not None and request['session_id'] != session:
        raise ValueError('invalid_live_result')
    if request['operation'] == 'collections':
        if set(result) != {'collections', 'truncated', 'session_id'}:
            raise ValueError('invalid_live_result')
        validate_collections({k: result[k] for k in ('collections', 'truncated')})
        return result
    if set(result) != {'photos', 'total_count', 'offset', 'has_more', 'previews', 'session_id'}:
        raise ValueError('invalid_live_result')
    photos = result['photos']
    if not isinstance(photos, list) or len(photos) > request['limit']:
        raise ValueError('invalid_live_result')
    if photos:
        for photo in photos:
            if not isinstance(photo, dict) or not set(photo) <= FIELDS | CONTEXT_FIELDS:
                raise ValueError('invalid_live_result')
            for key in set(photo) & CONTEXT_FIELDS:
                if not isinstance(photo[key], str) or len(photo[key]) > 80 or any(ord(c)<32 or ord(c)==127 for c in photo[key]):
                    raise ValueError('invalid_live_result')
        validate_selection({'photos': [{k:v for k,v in p.items() if k in FIELDS} for p in photos]}, require_base=True)
    count, offset = result['total_count'], result['offset']
    if (type(count) is not int or not 0 <= count <= 1000000
        or offset != request['offset'] or type(offset) is not int or offset > count
        or len(photos) != min(request['limit'], count - offset)
        or type(result['has_more']) is not bool or result['has_more'] != (offset + len(photos) < count)):
        raise ValueError('invalid_live_result')
    previews = result['previews']
    if not isinstance(previews, list) or len(previews) != (len(photos) if request['previews'] else 0):
        raise ValueError('invalid_live_result')
    cleaned = []
    for index, preview in enumerate(previews, 1):
        if not isinstance(preview, dict) or type(preview.get('index')) is not int or preview['index'] != index:
            raise ValueError('invalid_live_result')
        if set(preview) == {'index', 'jpeg'}:
            try:
                cleaned.append({'index': index, 'jpeg': clean_preview(preview['jpeg'])})
            except (OSError, ValueError) as exc:
                raise ValueError('invalid_preview') from exc
        elif set(preview) == {'index', 'error'} and isinstance(preview['error'], str) and preview['error'] in {'preview_unavailable', 'preview_timeout', 'preview_size_limit'}:
            cleaned.append(preview.copy())
        else:
            raise ValueError('invalid_live_result')
    return {**result, 'previews': cleaned}


@dataclass
class Job:
    request: dict
    deadline: float
    claimed: bool = False
    result: dict | None = None
    ready: threading.Event = field(default_factory=threading.Event)


class LiveReads:
    def __init__(self, enabled=False):
        self.enabled = enabled
        self.jobs = {}
        self.lock = threading.Lock()
        self.last_poll = 0

    def online(self):
        return self.enabled and self.last_poll > 0 and 0 <= time.monotonic() - self.last_poll < JOB_TTL

    def expire(self):
        for identifier, job in list(self.jobs.items()):
            if time.monotonic() >= job.deadline:
                job.result = {'error': 'live_timeout'}
                job.ready.set()
                del self.jobs[identifier]

    def submit(self, request):
        request = validate_request(request)
        with self.lock:
            self.expire()
            if not self.enabled:
                return {'error': 'live_reads_disabled'}
            if not self.online():
                return {'error': 'lightroom_worker_offline'}
            if len(self.jobs) >= 2:
                return {'error': 'live_busy'}
            identifier = uuid.uuid4().hex
            job = Job(request, time.monotonic() + JOB_TTL)
            self.jobs[identifier] = job
        try:
            if not job.ready.wait(JOB_TTL):
                return {'error': 'live_timeout'}
            return job.result
        finally:
            with self.lock:
                self.jobs.pop(identifier, None)

    def poll(self):
        with self.lock:
            self.expire()
            if not self.enabled:
                return {'error': 'live_reads_disabled'}
            self.last_poll = time.monotonic()
            for identifier, job in self.jobs.items():
                if not job.claimed:
                    job.claimed = True
                    return {'id': identifier, **job.request}
            return None

    def stop(self):
        with self.lock:
            self.last_poll = 0
            for job in self.jobs.values():
                job.result = {'error': 'worker_stopped'}
                job.ready.set()
            self.jobs.clear()
        return {'stopped': True}

    def reply(self, identifier, result):
        with self.lock:
            self.expire()
            job = self.jobs.get(identifier)
            if job is None or not job.claimed or job.ready.is_set():
                raise ValueError('unknown_live_request')
            request = job.request
        result = validate_result(request, result)
        with self.lock:
            if time.monotonic() >= job.deadline or self.jobs.get(identifier) is not job or job.ready.is_set():
                raise ValueError('unknown_live_request')
            job.result = copy.deepcopy(result)
            job.ready.set()
