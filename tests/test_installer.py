"""Install safety, native credential permissions and service plans."""
import json
import os
import plistlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from mhphotos_lightroom_mcp import installer
from mhphotos_lightroom_mcp.pairing import ensure_bridge_token, read_bridge_token
from mhphotos_lightroom_mcp.private_files import (
    private_directory,
    validate_private,
    windows_acl,
)
from mhphotos_lightroom_mcp.selection_transport import create_transfer, read_transfer


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path.resolve() / 'user space ü'
    path.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: path)
    return path


def test_token_created_privately_and_reused(home):
    path = home / '.mhphotos-lightroom-token'
    first = ensure_bridge_token(path)
    assert len(first) >= 64
    validate_private(path)
    before = path.stat().st_mtime_ns
    assert ensure_bridge_token(path) == first
    assert path.stat().st_mtime_ns == before


def test_unsafe_credential_is_never_repaired(home):
    path = home / '.mhphotos-lightroom-token'
    path.write_text('a' * 64)
    if os.name == 'nt':
        subprocess.run(['icacls.exe', str(path), '/grant', '*S-1-1-0:R'],
                       check=True, capture_output=True)
    else:
        path.chmod(0o644)
    before = path.read_bytes()
    with pytest.raises(PermissionError):
        ensure_bridge_token(path)
    assert path.read_bytes() == before


def test_symlink_and_hardlink_tokens_refused(home):
    target = home / 'original'
    ensure_bridge_token(target)
    hardlink = home / 'hardlink'
    os.link(target, hardlink)
    with pytest.raises(PermissionError):
        read_bridge_token(hardlink)
    hardlink.unlink()
    link = home / 'link'
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('Windows symlink privilege unavailable')
    with pytest.raises(PermissionError):
        ensure_bridge_token(link)


def test_transfer_native_permissions_and_limits(home):
    payload = {'photos': [{'fileName': 'IMG.CR3', 'rating': '3', 'pickStatus': '0'}]}
    path = create_transfer(payload)
    validate_private(path)
    assert read_transfer(path) == payload
    path.write_bytes(b'x' * 8193)
    with pytest.raises(ValueError, match='large'):
        read_transfer(path)
    with pytest.raises(ValueError, match='path'):
        read_transfer(home / 'selection-outside.json')


def test_transfer_unsafe_directory_refused(home):
    base = home / '.mhphotos-lightroom'
    private_directory(base)
    transfers = base / 'transfers'
    transfers.mkdir()
    if os.name == 'nt':
        # Replace ACL with a shared one instead of relying on runner inheritance.
        subprocess.run(['icacls.exe', str(transfers), '/grant', '*S-1-1-0:(OI)(CI)R'], check=True,
                       capture_output=True)
    else:
        transfers.chmod(0o755)
    with pytest.raises(PermissionError):
        create_transfer({'photos': []})


@pytest.mark.parametrize('platform', ['darwin', 'win32'])
def test_service_plans_have_no_secrets_and_no_elevation(home, platform):
    plan = installer.installation_plan(home, platform, 'test')
    assert plan['starts_service'] is False
    filename, data = installer.service_document(Path(plan['python']), platform, 'S-1-5-21-1234')
    assert b'TOKEN' not in data
    if platform == 'darwin':
        doc = plistlib.loads(data)
        assert doc['ProgramArguments'][-1] == 'bridge'
        assert doc['ProgramArguments'][0] == plan['python']
        assert filename.endswith('.plist')
    else:
        doc = ET.fromstring(data)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        assert doc.find('.//t:RunLevel', ns).text == 'LeastPrivilege'
        assert doc.find('.//t:LogonType', ns).text == 'InteractiveToken'


def test_windows_shell_expansion_refused():
    for bad in ('%PATH%', 'a!b', 'a&b', 'a"b', 'a\nb', 'a(b)'):
        with pytest.raises(ValueError):
            installer.windows_argument(bad)
    assert installer.windows_argument('C:/Users/First Last/ü') == '"C:/Users/First Last/ü"'


def test_invalid_install_names_and_platform(home):
    for name in ('../escape', '', 'a/b', 'a&b'):
        with pytest.raises(ValueError):
            installer.installation_plan(home, 'darwin', name)
    with pytest.raises(ValueError):
        installer.installation_plan(home, 'linux', 'test')


@pytest.fixture
def fake_runtime(monkeypatch):
    def create(self, path):
        path.mkdir()
    monkeypatch.setattr(installer.venv.EnvBuilder, 'create', create)
    calls = []
    original_run = subprocess.run
    def run(args, **kwargs):
        if ('-m' in args and 'pip' in args) or '-c' in args:
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)
        return original_run(args, **kwargs)
    monkeypatch.setattr(installer.subprocess, 'run', run)
    return calls


def test_install_is_side_by_side_and_reuses_existing_token(home, fake_runtime):
    # Mock external package install only, exercise real files/ACLs otherwise.
    platform = 'win32' if os.name == 'nt' else 'darwin'
    token = home / '.mhphotos-lightroom-token'
    ensure_bridge_token(token)
    before = token.read_bytes()
    source = Path(__file__).resolve().parents[1]
    result = installer.install(source, home, platform, 'safe')
    root = Path(result['root'])
    assert (root / 'READY').is_file()
    assert token.read_bytes() == before
    assert not (home / 'Library/LaunchAgents').exists()
    assert not (home / '.local/bin/mhphotos-lightroom-send').exists()
    assert len(fake_runtime) == 2
    assert all(isinstance(call, list) for call in fake_runtime)
    assert '-I' in fake_runtime[0]
    assert (Path(result['plugin']) / 'Installation.lua').is_file()
    assert json.loads((root / 'installation.json').read_text()) == result
    with pytest.raises(FileExistsError):
        installer.install(source, home, platform, 'safe')


def test_failed_install_leaves_inert_marker(home, monkeypatch):
    platform = 'win32' if os.name == 'nt' else 'darwin'
    def fail(*args):
        raise RuntimeError('simulated failure')
    monkeypatch.setattr(installer.venv.EnvBuilder, 'create', fail)
    with pytest.raises(RuntimeError):
        installer.install(Path(__file__).resolve().parents[1], home, platform, 'failed')
    root = home / '.mhphotos-lightroom/installations/failed'
    assert (root / 'FAILED').exists()
    assert not (root / 'READY').exists()
    assert not (home / '.mhphotos-lightroom-token').exists()


def test_plan_cli_is_read_only(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(sys, 'argv', ['install', '--name', 'preview'])
    installer.main()
    assert json.loads(capsys.readouterr().out)['starts_service'] is False
    assert list(home.iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='native Windows ACL check')
def test_windows_acl_failure_is_closed(home, monkeypatch):
    path = home / 'file'
    path.touch()
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess([], 1))
    with pytest.raises(PermissionError):
        windows_acl(path)


def test_local_credentials_bypass_environment_proxy(home, monkeypatch):
    import threading

    from mhphotos_lightroom_mcp import selection_sender
    from mhphotos_lightroom_mcp.bridge import BridgeConfig, read_selection
    from mhphotos_lightroom_mcp.local_bridge import make_server

    token = ensure_bridge_token(home / '.mhphotos-lightroom-token')
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:1')
    monkeypatch.setenv('no_proxy', '')
    monkeypatch.setenv('NO_PROXY', '')
    server = make_server(token, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        monkeypatch.setattr(selection_sender, 'BRIDGE_URL',
                            f'http://127.0.0.1:{server.server_port}/v1/selection')
        photos = [{'fileName': 'PRIVATE.CR3', 'rating': '5', 'pickStatus': '0'}]
        assert selection_sender.send_selection({'photos': photos}) == {'accepted': 1}
        assert read_selection(BridgeConfig(token=token, port=server.server_port)) == {'photos': photos}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
