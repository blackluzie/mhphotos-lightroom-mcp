"""Stateless metadata comparison for read-only preview positions, never identity."""
import hashlib
import json
import re

from mhphotos_lightroom_mcp.live_reads import validate_request, validate_result


def validate_candidate_request(path, offset, session_id, fingerprint):
    request = {'operation': 'collection_photos', 'path': path, 'offset': offset,
               'limit': 1, 'previews': True, 'session_id': session_id}
    validate_request(request)
    if (session_id is None or not isinstance(fingerprint, str)
        or not re.fullmatch('[0-9a-f]{64}', fingerprint)):
        raise ValueError('invalid_candidate_reference')
    return request


def metadata_fingerprint(path, session_id, offset, photo):
    request = validate_candidate_request(path, offset, session_id, '0' * 64)
    request['previews'] = False
    validate_result(request, {'photos': [photo], 'total_count': offset + 1,
                             'offset': offset, 'has_more': False, 'previews': [],
                             'session_id': session_id})
    data = {'collection_path': path, 'session_id': session_id,
            'collection_offset': offset, 'metadata': photo}
    encoded = json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
    return hashlib.sha256(encoded).hexdigest()
