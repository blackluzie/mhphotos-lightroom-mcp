"""Repeatable user-level macOS setup. Plan by default; never replace secrets."""
import argparse
import json
import os
import plistlib
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from .oauth_http import validate_redirect


def private_file(path):
    if path.is_symlink():
        raise ValueError(f'Symlink refused: {path}')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError(f'Expected an owner-controlled regular file with mode 600: {path}')
    return path.read_bytes()


def write_private(path, data):
    """Create absent files exclusively. Existing secrets/configs are retained."""
    if path.exists() or path.is_symlink():
        private_file(path)
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    return True


def make_agent(label, module, python, repo, home, exports):
    state = home / 'Library/Application Support/MHPhotosLightroom'
    log = home / 'Library/Logs/MHPhotos' / ('http.log' if module == 'http_service' else 'assets.log')
    return {'Label': label, 'ProgramArguments': ['/bin/sh', '-c',
        'exec ' + shlex.quote(str(python)) + ' -m mhphotos_lightroom_mcp.' + module],
        'WorkingDirectory': str(state), 'RunAtLoad': True, 'KeepAlive': True,
        'ProcessType': 'Interactive', 'StandardOutPath': str(log), 'StandardErrorPath': str(log),
        'EnvironmentVariables': {'PYTHONPATH': str(repo / 'server'), 'PYTHONUNBUFFERED': '1',
            'MHPHOTOS_EXPORT_DIR': str(exports)}}


def configure(home, repo, python, public_url=None, callbacks=(), secret_source=None, apply=False):
    from urllib.parse import urlsplit
    config_dir = home / '.config/mhphotos-lightroom'
    config_path = config_dir / 'desktop.json'
    existing = json.loads(private_file(config_path)) if config_path.exists() else {}
    issuer = public_url or existing.get('public_url', '')
    parsed = urlsplit(issuer)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query
            or parsed.fragment or parsed.username or parsed.password or parsed.port):
        raise ValueError('A public HTTPS origin without path or port is required')
    redirects = list(dict.fromkeys(existing.get('redirect_uris', []) + list(callbacks)))
    if not redirects:
        raise ValueError('At least one exact --callback is required')
    for uri in redirects:
        validate_redirect(uri)
    if existing.get('public_url') and existing['public_url'] != issuer:
        raise ValueError('Refusing to change an existing issuer; migrate explicitly')
    config = {**existing, 'public_url': issuer, 'redirect_uris': redirects}
    secret_path = config_dir / 'github-client-secret'
    if secret_path.exists():
        if not private_file(secret_path).strip():
            raise ValueError('Existing GitHub secret is empty')
    elif secret_source is None or not private_file(secret_source).strip():
        raise ValueError('Restore GitHub secret using --github-secret-file (mode 600)')
    for directory in (config_dir, home / '.mhphotos-lightroom-mcp'):
        if directory.is_symlink():
            raise ValueError(f'Symlink directory refused: {directory}')
        if directory.exists():
            info = directory.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError(f'Private directory must be owned and not writable by others: {directory}')
    for path in (config_dir / 'automation-token', home / '.mhphotos-lightroom-mcp/bridge.json'):
        if path.exists() or path.is_symlink():
            private_file(path)
    exports = home / 'Library/Application Support/MHPhotosLightroom/Exports'
    agents = {f'de.mhphotos.lightroom.{suffix}': make_agent(f'de.mhphotos.lightroom.{suffix}', module,
        python, repo, home, exports) for suffix, module in [('http', 'http_service'), ('assets', 'asset_delivery')]}
    plan = {'public_mcp_url': issuer + '/mcp', 'callbacks': redirects,
        'export_directory': str(exports), 'agents': list(agents), 'apply': apply,
        'routing': 'Preserve existing Tailscale routes; configure explicitly using docs/rebuild.md'}
    if not apply:
        return plan
    for directory in (config_dir, home / '.mhphotos-lightroom-mcp', exports,
            home / 'Library/LaunchAgents', home / 'Library/Logs/MHPhotos'):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_dir.chmod(0o700)
    (home / '.mhphotos-lightroom-mcp').chmod(0o700)
    if not secret_path.exists():
        write_private(secret_path, private_file(secret_source))
    payload = (json.dumps(config, indent=2) + '\n').encode()
    if existing != config:
        if config_path.exists():
            backup = config_path.with_name('desktop.before-rebuild.' + secrets.token_hex(4) + '.json')
            write_private(backup, private_file(config_path))
            temp = config_path.with_name('desktop.' + secrets.token_hex(4) + '.tmp')
            write_private(temp, payload)
            temp.replace(config_path)
        else:
            write_private(config_path, payload)
    write_private(config_dir / 'automation-token', secrets.token_hex(32).encode())
    write_private(home / '.mhphotos-lightroom-mcp/bridge.json', json.dumps({
        'url': 'http://127.0.0.1:47831', 'token': secrets.token_hex(32)}).encode())
    for label, agent in agents.items():
        path = home / 'Library/LaunchAgents' / (label + '.plist')
        data = plistlib.dumps(agent)
        if path.exists():
            current = private_file(path)
            if current == data:
                continue
            old = plistlib.loads(current)
            if old.get('Label') != label:
                raise ValueError('Refusing to replace unrelated LaunchAgent')
            write_private(path.with_name(label + '.before-rebuild.' + secrets.token_hex(4) + '.plist'), current)
            temp = path.with_name(label + '.' + secrets.token_hex(4) + '.tmp')
            write_private(temp, data)
            temp.replace(path)
        else:
            write_private(path, data)
    return plan


def routing_commands(status, hostname, binary):
    """Refuse conflicting listeners instead of replacing other applications."""
    commands = []
    web = status.get('Web', {})
    funnel = status.get('AllowFunnel', {})
    if any(key.endswith(':9443') and enabled for key, enabled in funnel.items()):
        raise ValueError('Asset port 9443 is public; resolve this explicitly before installation')
    for port, upstream in [(443, 8766), (9443, 8768)]:
        key = f'{hostname}:{port}'
        existing = web.get(key, {}).get('Handlers', {})
        tcp = status.get('TCP', {}).get(str(port))
        if any(k.endswith(':' + str(port)) and k != key for k in web):
            raise ValueError(f'HTTPS port {port} belongs to another host')
        if existing and existing != {'/': {'Proxy': f'http://127.0.0.1:{upstream}'}}:
            raise ValueError(f'HTTPS port {port} contains another route')
        if tcp and (not tcp.get('HTTPS') or not existing):
            raise ValueError(f'Port {port} has a conflicting TCP listener')
        if not existing or (port == 443 and not funnel.get(key)):
            commands.append([binary, 'funnel' if port == 443 else 'serve', '--bg',
                f'--https={port}', f'http://127.0.0.1:{upstream}'])
    return commands


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--public-url')
    parser.add_argument('--callback', action='append', default=[])
    parser.add_argument('--github-secret-file', type=Path)
    parser.add_argument('--configure-routing', action='store_true', help='Validate/create only MCP 443 and private assets 9443 Tailscale routes')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--start', action='store_true', help='Restart only the two managed services; requires --apply')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('The desktop deployment requires macOS')
    if args.start and not args.apply:
        parser.error('--start requires --apply')
    home = Path.home()
    repo = Path(__file__).resolve().parents[2]
    result = configure(home, repo, Path(sys.executable), args.public_url, args.callback,
        args.github_secret_file, False)
    commands = []
    if args.configure_routing:
        from urllib.parse import urlsplit
        binary = shutil.which('tailscale') or '/Applications/Tailscale.app/Contents/MacOS/Tailscale'
        status = json.loads(subprocess.run([binary, 'serve', 'status', '--json'], check=True, capture_output=True, text=True).stdout)
        commands = routing_commands(status, urlsplit(result['public_mcp_url']).hostname, binary)
        result['routing_commands'] = commands
    if args.apply:
        configure(home, repo, Path(sys.executable), args.public_url, args.callback, args.github_secret_file, True)
        result["apply"] = True
    print(json.dumps(result, indent=2))
    if args.start:
        for label in result['agents']:
            domain = f'gui/{os.getuid()}'
            path = home / 'Library/LaunchAgents' / (label + '.plist')
            loaded = subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True, check=False).returncode == 0
            if loaded:
                subprocess.run(['launchctl', 'bootout', domain + '/' + label], check=True)
            subprocess.run(['launchctl', 'enable', domain + '/' + label], check=True)
            for attempt in range(5):
                started = subprocess.run(['launchctl', 'bootstrap', domain, str(path)],
                    capture_output=True, text=True, check=False)
                if started.returncode == 0:
                    break
                # macOS can briefly retain the previous job after successful bootout.
                time.sleep(0.25 * (attempt + 1))
            else:
                raise RuntimeError('Could not load managed service: ' + label + ': ' + started.stderr)

        print('Managed services started. Reconnect OAuth clients after the HTTP restart.')
    if args.apply:
        for command in commands:
            subprocess.run(command, check=True)
        target = home / 'Documents/MHPhotos-Lightroom-Plugin/mhphotos.lrdevplugin'
        if not target.exists():
            shutil.copytree(repo / 'plugin/mhphotos.lrdevplugin', target)
            print('Plugin copied. Add it once in Lightroom Plug-in Manager:', target)
        else:
            print('Existing plugin preserved:', target)


if __name__ == '__main__':
    main()
