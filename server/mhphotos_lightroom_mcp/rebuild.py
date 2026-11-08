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

from .http_service import mhphotos_url, webhook_url
from .oauth_http import github_settings, validate_redirect


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


def configure(home, repo, python, public_url=None, callbacks=(), secret_source=None, apply=False, github=None,
        n8n_webhook_url=None, allow_writes=None, mhphotos_site=None, allow_instagram=None, hot_folder=None, allow_file_edits=None,
        allow_publish=None):
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
    github = {key: value for key, value in (github or {}).items() if value}
    for key in ('client_id', 'owner_id', 'login'):
        name = 'github_' + key
        if existing.get(name) and github.get(key) and str(github[key]) != str(existing[name]):
            raise ValueError(f'Refusing to change the existing {name}; migrate explicitly')
    owner = github_settings(github.get('client_id') or existing.get('github_client_id'),
        github.get('owner_id') or existing.get('github_owner_id'),
        github.get('login') or existing.get('github_login'))
    config = {**existing, 'public_url': issuer, 'redirect_uris': redirects,
        'github_client_id': owner['client_id'], 'github_owner_id': owner['owner_id']}
    if owner['login']:
        config['github_login'] = owner['login']
    if n8n_webhook_url:
        config['n8n_webhook_url'] = webhook_url(n8n_webhook_url)
    if allow_writes is not None:
        config['allow_writes'] = bool(allow_writes)
    if mhphotos_site:
        config['mhphotos_url'] = mhphotos_url(mhphotos_site)
    if hot_folder is not None:
        if hot_folder is False:
            config.pop('hot_folder', None)
        else:
            folder = Path(os.path.expanduser(str(hot_folder)))
            if not folder.is_absolute():
                raise ValueError('--hot-folder needs an absolute path')
            if not (n8n_webhook_url or config.get('n8n_webhook_url')):
                raise ValueError('--hot-folder needs --n8n-webhook-url (n8n uploads the files)')
            config['hot_folder'] = str(folder)
    if allow_instagram is not None:
        if allow_instagram and not config.get('mhphotos_url'):
            raise ValueError('--allow-instagram needs --mhphotos-url')
        config['allow_instagram'] = bool(allow_instagram)
    if allow_publish is not None:
        if allow_publish and not config.get('mhphotos_url'):
            raise ValueError('--allow-publish needs --mhphotos-url')
        config['allow_publish'] = bool(allow_publish)
    if allow_file_edits is not None:
        if allow_file_edits and not (allow_writes or config.get('allow_writes')):
            raise ValueError('--allow-file-edits needs --allow-writes')
        config['allow_file_edits'] = bool(allow_file_edits)
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
        'n8n_webhook_url': config.get('n8n_webhook_url'), 'allow_writes': config.get('allow_writes', False),
        'mhphotos_url': config.get('mhphotos_url'), 'allow_instagram': config.get('allow_instagram', False),
        'hot_folder': config.get('hot_folder'), 'allow_file_edits': config.get('allow_file_edits', False),
        'allow_publish': config.get('allow_publish', False),
        **({'warning': 'The old inbox watcher (de.mhphotos.inbox-watcher) also watches a hot folder and uploads '
            'directly. Stop it so each JPEG takes one path: launchctl bootout gui/$(id -u)/de.mhphotos.inbox-watcher'}
           if config.get('hot_folder') and (home / 'Library/LaunchAgents/de.mhphotos.inbox-watcher.plist').exists()
           else {}),
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
    if config.get('n8n_webhook_url'):
        write_private(config_dir / 'n8n-webhook-token', secrets.token_hex(32).encode())
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
    parser.add_argument('--github-client-id', help='Client id of your GitHub OAuth app')
    parser.add_argument('--github-owner-id', help='Numeric GitHub account id allowed to sign in')
    parser.add_argument('--github-login', help='Optional GitHub login, only pre-filled on the sign-in page')
    parser.add_argument('--n8n-webhook-url', help='Optional n8n webhook (https) told about each verified export')
    parser.add_argument('--allow-writes', dest='allow_writes', action='store_true', default=None,
        help='Offer catalog edits (ratings, flags, develop, presets) to remote clients; never deletes or publishes')
    parser.add_argument('--mhphotos-url', help='https origin of the MHPhotos site; offers Instagram/inbox reads '
        '(admin token from the Keychain entry mhphotos-admin-token)')
    parser.add_argument('--no-allow-writes', dest='allow_writes', action='store_false',
        help='Remote clients may only read and export again')
    parser.add_argument('--allow-instagram', dest='allow_instagram', action='store_true', default=None,
        help='Offer planning Instagram posts in the MHPhotos social queue (published automatically at the planned time)')
    parser.add_argument('--no-allow-instagram', dest='allow_instagram', action='store_false',
        help='Remove Instagram planning again')
    parser.add_argument('--allow-publish', dest='allow_publish', action='store_true', default=None,
        help='Offer publishing MHPhotos inbox items on the public site and editing published photos (dry run first)')
    parser.add_argument('--no-allow-publish', dest='allow_publish', action='store_false',
        help='Remove publishing on the site again')
    parser.add_argument('--hot-folder', nargs='?', const=str(Path.home() / 'Pictures/MHPhotos-Inbox'), default=None,
        help="Folder of Lightroom's auto-export; finished JPEGs go through n8n into the MHPhotos inbox "
             '(default ~/Pictures/MHPhotos-Inbox)')
    parser.add_argument('--no-hot-folder', dest='hot_folder', action='store_false', help='Stop watching the hot folder')
    parser.add_argument('--allow-file-edits', dest='allow_file_edits', action='store_true', default=None,
        help='Offer writing a capture time into JPEG files (exiftool, backups first); needs --allow-writes')
    parser.add_argument('--no-allow-file-edits', dest='allow_file_edits', action='store_false',
        help='Remove file edits again')
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
    github = {'client_id': args.github_client_id, 'owner_id': args.github_owner_id, 'login': args.github_login}
    result = configure(home, repo, Path(sys.executable), args.public_url, args.callback,
        args.github_secret_file, False, github, args.n8n_webhook_url, args.allow_writes, args.mhphotos_url,
        args.allow_instagram, args.hot_folder, args.allow_file_edits, args.allow_publish)
    commands = []
    if args.configure_routing:
        from urllib.parse import urlsplit
        binary = shutil.which('tailscale') or '/Applications/Tailscale.app/Contents/MacOS/Tailscale'
        status = json.loads(subprocess.run([binary, 'serve', 'status', '--json'], check=True, capture_output=True, text=True).stdout)
        commands = routing_commands(status, urlsplit(result['public_mcp_url']).hostname, binary)
        result['routing_commands'] = commands
    if args.apply:
        configure(home, repo, Path(sys.executable), args.public_url, args.callback, args.github_secret_file, True, github,
            args.n8n_webhook_url, args.allow_writes, args.mhphotos_url, args.allow_instagram, args.hot_folder,
            args.allow_file_edits, args.allow_publish)
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
            # The old process may need up to ~10 s to close open client connections.
            for attempt in range(20):
                started = subprocess.run(['launchctl', 'bootstrap', domain, str(path)],
                    capture_output=True, text=True, check=False)
                if started.returncode == 0:
                    break
                # macOS can briefly retain the previous job after successful bootout.
                time.sleep(1)
            else:
                raise RuntimeError('Could not load managed service: ' + label + ': ' + started.stderr)

        print('Managed services started. Reconnect OAuth clients after the HTTP restart.')
    if args.apply:
        for command in commands:
            subprocess.run(command, check=True)
        print(install_plugin(repo / 'plugin/mhphotos.lrdevplugin',
            home / 'Documents/MHPhotos-Lightroom-Plugin/mhphotos.lrdevplugin'))


def plugin_version(plugin: Path) -> str | None:
    try:
        text = (plugin / 'Version.lua').read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith('return '):
            return line.split("'")[1] if "'" in line else None
    return None


def install_plugin(source: Path, target: Path) -> str:
    """Copy the plugin on first run; replace it when the repository has another version.

    Settings live in ~/.mhphotos-lightroom-mcp, never inside the plugin folder, so replacing it
    loses nothing. The old folder is swapped out only after the new copy is complete.
    """
    if target.is_symlink():
        return f'Plugin is a symlink, left as it is: {target}'
    if not target.exists():
        shutil.copytree(source, target)
        return f'Plugin copied. Add it once in Lightroom Plug-in Manager: {target}'
    old, new = plugin_version(target), plugin_version(source)
    if old == new:
        return f'Plugin {old} is current: {target}'
    staging, retired = target.with_name(target.name + '.new'), target.with_name(target.name + '.old')
    for leftover in (staging, retired):
        if leftover.exists():
            shutil.rmtree(leftover)
    shutil.copytree(source, staging)
    target.rename(retired)
    staging.rename(target)
    shutil.rmtree(retired)
    return (f'Plugin updated {old} -> {new}: {target}. In Lightroom: Plug-in Manager -> select it -> '
            'Reload Plug-in (or restart Lightroom).')


if __name__ == '__main__':
    main()
