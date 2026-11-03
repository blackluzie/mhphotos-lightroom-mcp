import json
import plistlib

import pytest
from mhphotos_lightroom_mcp.rebuild import configure

GITHUB = {'client_id': 'app-id', 'owner_id': '42', 'login': 'owner'}


def setup(tmp_path):
    home = tmp_path / 'home with spaces'
    home.mkdir()
    secret = tmp_path / 'restored-secret'
    secret.write_text('synthetic-only')
    secret.chmod(0o600)
    return home, secret


def test_plan_no_effects_and_apply_is_repeatable(tmp_path):
    home, secret = setup(tmp_path)
    def run(apply=False, callbacks=('https://client.example/callback',)):
        return configure(home, tmp_path, tmp_path / 'env with spaces/bin/python',
            'https://photos.example', callbacks, secret, apply, GITHUB)
    run()
    assert list(home.iterdir()) == []
    run(True)
    bridge = home / '.mhphotos-lightroom-mcp/bridge.json'
    token = home / '.config/mhphotos-lightroom/automation-token'
    before = (bridge.read_bytes(), token.read_bytes())
    run(True)
    assert before == (bridge.read_bytes(), token.read_bytes())
    assert token.stat().st_mode & 0o777 == 0o600
    plist = home / 'Library/LaunchAgents/de.mhphotos.lightroom.http.plist'
    agent = plistlib.loads(plist.read_bytes())
    assert agent['ProgramArguments'][2].startswith("exec '")
    assert agent['EnvironmentVariables']['MHPHOTOS_EXPORT_DIR'].endswith('/Exports')
    run(True, ('https://claude.ai/api/mcp/auth_callback',))
    conf = json.loads((home / '.config/mhphotos-lightroom/desktop.json').read_text())
    assert conf['redirect_uris'] == ['https://client.example/callback', 'https://claude.ai/api/mcp/auth_callback']
    assert (conf['github_client_id'], conf['github_owner_id'], conf['github_login']) == ('app-id', '42', 'owner')
    assert len(list((home / '.config/mhphotos-lightroom').glob('desktop.before-rebuild.*.json'))) == 1
    assert before == (bridge.read_bytes(), token.read_bytes())


def test_refuses_issuer_change_and_unsafe_secret(tmp_path):
    home, secret = setup(tmp_path)
    secret.chmod(0o644)
    with pytest.raises(ValueError, match='mode 600'):
        configure(home, tmp_path, tmp_path / 'python', 'https://photos.example',
            ['https://client.example/callback'], secret, True, GITHUB)
    secret.chmod(0o600)
    configure(home, tmp_path, tmp_path / 'python', 'https://photos.example',
        ['https://client.example/callback'], secret, True, GITHUB)
    with pytest.raises(ValueError, match='issuer'):
        configure(home, tmp_path, tmp_path / 'python', 'https://different.example', [], secret, True)


def test_does_not_replace_symlink_secret(tmp_path):
    home, secret = setup(tmp_path)
    config = home / '.config/mhphotos-lightroom'
    config.mkdir(parents=True, mode=0o700)
    (config / 'github-client-secret').symlink_to(secret)
    with pytest.raises(ValueError, match='Symlink'):
        configure(home, tmp_path, tmp_path / 'python', 'https://photos.example',
            ['https://client.example/callback'], secret, True, GITHUB)


def test_routes_are_repeatable_and_preserve_other_apps():
    from mhphotos_lightroom_mcp.rebuild import routing_commands
    status = {'Web': {'other:8443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9999'}}}}}
    commands = routing_commands(status, 'photos.example', 'tailscale')
    assert [c[1] for c in commands] == ['funnel', 'serve']
    assert [c[3] for c in commands] == ['--https=443', '--https=9443']
    status = {'TCP': {'443': {'HTTPS': True}, '9443': {'HTTPS': True}},
        'Web': {f'photos.example:{port}': {'Handlers': {'/': {'Proxy': f'http://127.0.0.1:{upstream}'}}}
            for port, upstream in [(443, 8766), (9443, 8768)]},
        'AllowFunnel': {'photos.example:443': True}}
    assert routing_commands(status, 'photos.example', 'tailscale') == []
    status['AllowFunnel']['photos.example:9443'] = True
    with pytest.raises(ValueError, match='public'):
        routing_commands(status, 'photos.example', 'tailscale')
    status['AllowFunnel'].pop('photos.example:9443')
    status['Web']['photos.example:443']['Handlers']['/']['Proxy'] = 'http://127.0.0.1:9999'
    with pytest.raises(ValueError, match='another route'):
        routing_commands(status, 'photos.example', 'tailscale')


def test_github_owner_is_required_and_not_silently_changed(tmp_path):
    home, secret = setup(tmp_path)
    with pytest.raises(ValueError, match='client id'):
        configure(home, tmp_path, tmp_path / 'python', 'https://photos.example',
            ['https://client.example/callback'], secret, True)
    configure(home, tmp_path, tmp_path / 'python', 'https://photos.example',
        ['https://client.example/callback'], secret, True, GITHUB)
    # Later runs may omit the values; they are kept from desktop.json.
    configure(home, tmp_path, tmp_path / 'python', 'https://photos.example', [], secret, True)
    with pytest.raises(ValueError, match='github_owner_id'):
        configure(home, tmp_path, tmp_path / 'python', 'https://photos.example', [], secret, True,
            {**GITHUB, 'owner_id': '43'})


def test_n8n_webhook_creates_its_own_token(tmp_path):
    home, secret = setup(tmp_path)
    configure(home, tmp_path, tmp_path / 'python', 'https://photos.example',
        ['https://client.example/callback'], secret, True, GITHUB)
    token = home / '.config/mhphotos-lightroom/n8n-webhook-token'
    assert not token.exists()
    plan = configure(home, tmp_path, tmp_path / 'python', 'https://photos.example', [], secret, True,
        None, 'https://n8n.example/webhook/mhphotos-export-ready')
    assert plan['n8n_webhook_url'] == 'https://n8n.example/webhook/mhphotos-export-ready'
    assert token.stat().st_mode & 0o777 == 0o600 and len(token.read_text()) == 64
    before = token.read_bytes()
    configure(home, tmp_path, tmp_path / 'python', 'https://photos.example', [], secret, True)
    assert token.read_bytes() == before
    conf = json.loads((home / '.config/mhphotos-lightroom/desktop.json').read_text())
    assert conf['n8n_webhook_url'] == 'https://n8n.example/webhook/mhphotos-export-ready'
    with pytest.raises(ValueError, match='https'):
        configure(home, tmp_path, tmp_path / 'python', 'https://photos.example', [], secret, False,
            None, 'http://n8n.example/hook')
