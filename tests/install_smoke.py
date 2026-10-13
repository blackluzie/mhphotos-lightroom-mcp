"""Real native install smoke test, using only an isolated temporary home."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mhphotos_lightroom_mcp.installer import install


def main():
    if sys.platform not in {'darwin', 'win32'}:
        return
    with tempfile.TemporaryDirectory(prefix='mhphotos-smoke-') as directory:
        home = Path(directory).resolve() / 'user space ü'
        home.mkdir()
        plan = install(Path(__file__).resolve().parents[1], home, sys.platform, 'smoke')
        root = Path(plan['root'])
        assert (root / 'READY').is_file()
        # Installed runtime exercises the full helper-to-HTTP snapshot on an
        # ephemeral port, so the user's live bridge is never contacted.
        code = '''
import json, threading
from pathlib import Path
from mhphotos_lightroom_mcp.pairing import read_bridge_token
from mhphotos_lightroom_mcp.local_bridge import make_server
from mhphotos_lightroom_mcp.selection_transport import create_transfer, read_transfer
from mhphotos_lightroom_mcp import selection_sender
home = Path(__import__('sys').argv[1])
Path.home = classmethod(lambda cls: home)
token = read_bridge_token(home / '.mhphotos-lightroom-token')
server = make_server(token, port=0)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    selection_sender.BRIDGE_URL = f'http://127.0.0.1:{server.server_port}/v1/selection'
    payload = {'photos': [{'fileName': 'SAMPLE.CR3', 'rating': '5', 'pickStatus': '0'}]}
    path = create_transfer(payload)
    assert selection_sender.send_selection(read_transfer(path)) == {'accepted': 1}
    path.unlink()
    assert server.selection == payload['photos']
finally:
    server.shutdown(); server.server_close(); thread.join(timeout=3)
print('Isolated installation and read-only transfer verified')
'''
        subprocess.run([plan['python'], '-I', '-c', code, str(home)], check=True,
                       env=dict(os.environ, PYTHONPATH=''))


if __name__ == '__main__':
    main()
