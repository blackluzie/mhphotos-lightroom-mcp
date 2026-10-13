"""Explicit, side-by-side user installation; never register or restart services."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import uuid
import venv
import xml.etree.ElementTree as ET
from pathlib import Path

from mhphotos_lightroom_mcp.pairing import ensure_bridge_token
from mhphotos_lightroom_mcp.private_files import (
    private_directory,
    reject_links,
    validate_private,
    write_new_private,
)


def windows_argument(value: str) -> str:
    # LrTasks.execute uses the Windows command processor. Refuse expansion and
    # metacharacters even inside quotes; never try to escape untrusted commands.
    if any(c in value for c in '\"%!&|<>^()\r\n') or value.endswith('\\'):
        raise ValueError('Unsupported character in Windows installation path')
    return '"' + value + '"'


def installation_plan(home: Path, platform: str, name: str) -> dict:
    if platform not in {'darwin', 'win32'}:
        raise ValueError('Installer supports macOS and Windows only')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{0,63}', name):
        raise ValueError('Invalid installation name')
    if platform == 'win32' and name.upper() in {
        'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
        *(f'LPT{i}' for i in range(1, 10)),
    }:
        raise ValueError('Reserved Windows installation name')
    root = home / '.mhphotos-lightroom' / 'installations' / name
    python = root / 'venv' / ('Scripts/python.exe' if platform == 'win32' else 'bin/python')
    if platform == 'win32':
        windows_argument(str(python))
        windows_argument(str(home / '.mhphotos-lightroom' / 'transfers'))
    return {'root': str(root), 'python': str(python), 'platform': platform,
            'token': str(home / '.mhphotos-lightroom-token'),
            'plugin': str(root / 'MHPhotosLightroom.lrplugin'),
            'starts_service': False, 'changes_existing_plugin': False}


def service_document(python: Path, platform: str, owner_sid: str | None = None) -> tuple[str, bytes]:
    arguments = [str(python), '-I', '-m', 'mhphotos_lightroom_mcp.desktop', 'bridge']
    if platform == 'darwin':
        return 'org.mhphotos.lightroom.bridge.plist', plistlib.dumps({
            'Label': 'org.mhphotos.lightroom.bridge', 'ProgramArguments': arguments,
            'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
        })
    if not owner_sid or not re.fullmatch(r'S-1-[0-9-]+', owner_sid):
        raise ValueError('Windows task requires the current user SID')
    ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', ns)
    task = ET.Element(f'{{{ns}}}Task', version='1.2')
    def child(parent, tag, value=None):
        element = ET.SubElement(parent, f'{{{ns}}}{tag}')
        if value is not None:
            element.text = value
        return element
    triggers = child(task, 'Triggers')
    logon = child(triggers, 'LogonTrigger')
    child(logon, 'Enabled', 'true')
    child(logon, 'UserId', owner_sid)
    principals = child(task, 'Principals')
    principal = child(principals, 'Principal'); principal.set('id', 'Author')
    child(principal, 'UserId', owner_sid)
    child(principal, 'LogonType', 'InteractiveToken')
    child(principal, 'RunLevel', 'LeastPrivilege')
    settings = child(task, 'Settings')
    child(settings, 'MultipleInstancesPolicy', 'IgnoreNew')
    child(settings, 'ExecutionTimeLimit', 'PT0S')
    actions = child(task, 'Actions'); actions.set('Context', 'Author')
    execute = child(actions, 'Exec')
    child(execute, 'Command', str(python))
    child(execute, 'Arguments', '-I -m mhphotos_lightroom_mcp.desktop bridge')
    return 'mhphotos-lightroom-bridge.xml', ET.tostring(task, encoding='utf-8', xml_declaration=True)


def install(source: Path, home: Path, platform: str, name: str) -> dict:
    plan = installation_plan(home, platform, name)
    root = Path(plan['root'])
    reject_links(home)
    if not home.is_dir():
        raise ValueError('User home is unavailable')
    reject_links(root)
    if root.exists():
        raise FileExistsError('Installation exists; choose a new name')
    plugin_source = source / 'lightroom/MHPhotosLightroom.lrplugin'
    if not (source / 'pyproject.toml').is_file() or not (plugin_source / 'Info.lua').is_file():
        raise ValueError('Install from a complete, reviewed source checkout')
    # Check legacy credentials before any install work, but never rotate them.
    token = Path(plan['token'])
    if token.exists() or token.is_symlink():
        from mhphotos_lightroom_mcp.pairing import read_bridge_token
        read_bridge_token(token)
    base = home / '.mhphotos-lightroom'
    private_directory(base)
    private_directory(base / 'transfers')
    private_directory(base / 'installations')
    # Atomic reservation protects against concurrent installers. A failed install
    # leaves this private directory for inspection; it can never be activated.
    root.mkdir(mode=0o700)
    if os.name == 'nt':
        from mhphotos_lightroom_mcp.private_files import windows_acl
        windows_acl(root, protect=True)
    validate_private(root, directory=True)
    try:
        venv.EnvBuilder(with_pip=True, symlinks=False).create(root / 'venv')
        python = Path(plan['python'])
        subprocess.run([str(python), '-I', '-m', 'pip', '--isolated', 'install',
                        '--index-url', 'https://pypi.org/simple', str(source)], check=True)
        subprocess.run([str(python), '-I', '-c',
                        ('from mhphotos_lightroom_mcp.desktop import main; '
                         'from mcp.server.fastmcp import FastMCP')], check=True)
        plugin = Path(plan['plugin'])
        plugin.mkdir(mode=0o700)
        for item in plugin_source.glob('*.lua'):
            if item.is_symlink():
                raise ValueError('Linked plugin source refused')
            shutil.copyfile(item, plugin / item.name)
        # A Lua data file avoids shell launchers and secrets in the plugin.
        # json quoting is compatible for ordinary paths after slash normalization.
        executable = str(python).replace('\\', '/')
        write_new_private(plugin / 'Installation.lua',
                          ('return { python = ' + json.dumps(executable, ensure_ascii=False) + ' }\n').encode())
        # Plugin directory inherits private ACL on Windows, mode 700 on POSIX.
        ensure_bridge_token(token)
        owner_sid = None
        if platform == 'win32':
            powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            identity = subprocess.run(
                [str(powershell), '-NoProfile', '-NonInteractive', '-Command',
                 '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
                capture_output=True, text=True, check=True, timeout=20,
            )
            owner_sid = identity.stdout.strip()
        filename, data = service_document(python, platform, owner_sid)
        write_new_private(root / filename, data)
        write_new_private(root / 'installation.json', json.dumps(plan, indent=2).encode())
        write_new_private(root / 'READY', b'Installation verified; activation remains explicit.\n')
    except Exception:
        write_new_private(root / 'FAILED', b'Installation incomplete; do not activate.\n')
        raise
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path.cwd())
    parser.add_argument('--name', default='install-' + uuid.uuid4().hex[:12])
    parser.add_argument('--install', action='store_true', help='Install; default only prints a plan')
    args = parser.parse_args()
    if args.install:
        result = install(args.source.resolve(), Path.home(), sys.platform, args.name)
    else:
        result = installation_plan(Path.home(), sys.platform, args.name)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
