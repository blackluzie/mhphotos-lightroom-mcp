"""Keep the existing Classic plugin heartbeat connected; never enqueue commands."""
import json
import os
import signal
import stat
import threading
from pathlib import Path

from .bridge import Bridge


def main():
    path = Path.home() / '.mhphotos-lightroom-mcp/bridge.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode):
            raise ValueError('Bridge config must be owner-controlled, mode 600')
        config = json.loads(f.read(4096))
    if config.get('url') != 'http://127.0.0.1:47831':
        raise ValueError('Unexpected bridge URL; refusing to change existing configuration')
    token = config.get('token', '')
    if not isinstance(token, str) or len(token) < 32:
        raise ValueError('Invalid bridge token')
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    bridge = Bridge(token=token, port=47831)
    bridge.start()
    print('Classic heartbeat bridge listening on loopback:47831; no command producer', flush=True)
    try:
        stopped.wait()
    finally:
        bridge.stop()


if __name__ == '__main__':
    main()
