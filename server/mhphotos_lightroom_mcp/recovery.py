"""Authenticated encrypted backup/restore of Mac connection configuration, never photos."""
import argparse
import io
import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from .rebuild import private_file, write_private

FILES = (
    '.config/mhphotos-lightroom/desktop.json',
    '.config/mhphotos-lightroom/github-client-secret',
    '.config/mhphotos-lightroom/automation-token',
    '.mhphotos-lightroom-mcp/bridge.json',
)
DATABASE = '.config/mhphotos-lightroom/oauth-clients.sqlite3'


def backup(home, output, key_path):
    if output.exists() or output.is_symlink():
        raise ValueError('Backup destination already exists')
    payloads = {name: private_file(home / name) for name in FILES}
    if (home / DATABASE).exists():
        private_file(home / DATABASE)
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp) / 'clients.sqlite3'
            with (sqlite3.connect(f'file:{home / DATABASE}?mode=ro', uri=True) as source,
                    sqlite3.connect(snapshot) as dest):
                source.backup(dest)
            payloads[DATABASE] = snapshot.read_bytes()
    if not key_path.exists():
        write_private(key_path, Fernet.generate_key())
    cipher = Fernet(private_file(key_path).strip())
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name, data in payloads.items():
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    write_private(output, cipher.encrypt(stream.getvalue()))
    return len(payloads)


def restore(home, source, key_path):
    plaintext = Fernet(private_file(key_path).strip()).decrypt(private_file(source))
    payloads = {}
    with tarfile.open(fileobj=io.BytesIO(plaintext), mode='r:gz') as archive:
        for member in archive.getmembers():
            if (member.name not in (*FILES, DATABASE) or not member.isfile()
                    or member.name in payloads or member.size > 10_000_000):
                raise ValueError('Unexpected or unsafe recovery archive member')
            payloads[member.name] = archive.extractfile(member).read()
    if not set(FILES).issubset(payloads):
        raise ValueError('Incomplete recovery archive')
    # Validate every destination before the first write. Never replace live secrets.
    for name, data in payloads.items():
        path = home / name
        for parent in path.parents:
            if parent == home:
                break
            if parent.is_symlink():
                raise ValueError('Symlink recovery directory refused')
        if (path.exists() or path.is_symlink()) and private_file(path) != data:
            raise ValueError(f'Existing configuration differs; restore to a clean account: {path}')
    for name, data in payloads.items():
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(path, data)
    return len(payloads)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'restore'])
    parser.add_argument('--file', type=Path, required=True)
    parser.add_argument('--key-file', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    count = globals()[args.action](Path.home(), args.file, args.key_file)
    print(f'{args.action}: {count} configuration files. Key and photos are not inside the archive.')


if __name__ == '__main__':
    main()
