"""Private, non-executable job files for the Lightroom worker. Mac test only."""
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from mhphotos_lightroom_mcp.live_reads import MAX_LIVE_BODY, validate_request
from mhphotos_lightroom_mcp.pairing import read_bridge_token
from mhphotos_lightroom_mcp.private_files import validate_private, write_new_private
from mhphotos_lightroom_mcp.runtime_profile import (
    bridge_port,
    bridge_token_path,
    profile_root,
    transfer_path,
)


def post_live(route, payload, *, timeout=5):
    if profile_root() is None:
        raise ValueError('Live reads require the isolated profile')
    data = json.dumps(payload).encode()
    if len(data) > MAX_LIVE_BODY:
        raise ValueError('Live payload too large')
    token = read_bridge_token(bridge_token_path())
    request = Request(f'http://127.0.0.1:{bridge_port()}/v1/live/{route}', data=data,
                      headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
            raw = response.read(MAX_LIVE_BODY + 1)
    except HTTPError as exc:
        with exc:
            # Only the broker's exact stale-reply response is recoverable. Never
            # swallow authentication, schema, size, transport or other HTTP errors.
            if route == 'reply' and exc.code == 409:
                raw = exc.read(257)
                if len(raw) <= 256:
                    try:
                        stale = json.loads(raw)
                    except (ValueError, UnicodeError):
                        stale = None
                    if stale == {'error': 'live_request_unavailable'}:
                        return {'accepted': False, 'reason': 'live_request_unavailable'}
            raise

    if len(raw) > MAX_LIVE_BODY:
        raise ValueError('Live payload too large')
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError('Invalid live response')  # noqa: TRY004
    return result


def validate_path(path):
    if (not path.is_absolute() or path.parent != transfer_path()
        or not re.fullmatch(r'live-[0-9]{1,12}-[0-9]{6}\.txt', path.name)):
        raise ValueError('Invalid worker file')
    validate_private(path.parent, directory=True)


def poll(path):
    validate_path(path)
    if path.exists() or path.is_symlink():
        raise ValueError('Worker file already exists')
    result = post_live('poll', {})
    if set(result) != {'request'}:
        raise ValueError('Invalid poll response')
    job = result['request']
    content = ''
    if job is not None:
        if not isinstance(job, dict) or not isinstance(job.get('id'), str) or not re.fullmatch('[0-9a-f]{32}', job['id']):
            raise ValueError('Invalid job ID')
        request = validate_request({k:v for k,v in job.items() if k != 'id'})
        lines = [job['id'], request['operation'], str(request.get('offset',0)),
                 str(request.get('limit',0)), '1' if request.get('previews') else '0',
                 request.get('session_id') or '']
        lines.extend(segment.encode('utf-8').hex() for segment in request.get('path',[]))
        content = '\n'.join(lines)+'\n'
    write_new_private(path, content.encode())


def reply(path):
    validate_path(path)
    validate_private(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as handle:
            raw = handle.read(MAX_LIVE_BODY + 1)
        if len(raw) > MAX_LIVE_BODY:
            raise ValueError('Worker result too large')
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {'id','result'}:
            raise ValueError('Invalid result file')
        outcome = post_live('reply', envelope)
        if outcome != {'accepted': False, 'reason': 'live_request_unavailable'} and outcome.get('accepted') is not True:
            raise ValueError('Reply rejected')
    finally:
        path.unlink(missing_ok=True)


def main():
    try:
        if len(sys.argv) != 3 or sys.argv[1] not in {'poll','reply'}:
            raise ValueError('Invalid worker mode')
        (poll if sys.argv[1]=='poll' else reply)(Path(sys.argv[2]))
    except (OSError, ValueError):
        print('{"error":"worker_io_failed"}')
        sys.exit(1)


if __name__ == '__main__':
    main()
