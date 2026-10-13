"""Owner-only files; fail closed on unsafe existing state and Windows ACL errors."""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

# Paths are environment data, never PowerShell source. Use Windows PowerShell 5.1.
_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$p = $env:MHPHOTOS_PRIVATE_PATH
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
if ([System.IO.Directory]::Exists($p)) {
    $item = [System.IO.DirectoryInfo]::new($p)
} else {
    $item = [System.IO.FileInfo]::new($p)
    if (!$item.Exists) { throw 'Missing file' }
}
if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Reparse point' }
if ($env:MHPHOTOS_PRIVATE_SET -eq '1') {
    if ($item -is [System.IO.DirectoryInfo]) {
        $acl = [System.Security.AccessControl.DirectorySecurity]::new()
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    } else {
        $acl = [System.Security.AccessControl.FileSecurity]::new()
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow')
    }
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule($rule)
    $item.SetAccessControl($acl)
}
$acl = $item.GetAccessControl()
if ($acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'Owner' }
$rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
$allowed = $false
foreach ($rule in $rules) {
    if ($rule.AccessControlType -eq 'Allow') {
        if ($rule.IdentityReference.Value -ne $sid.Value) { throw 'Shared ACL' }
        $allowed = $true
    }
}
if (!$allowed) { throw 'Missing owner access' }
"""


def windows_acl(path: Path, *, protect: bool = False) -> None:
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run(
        [str(executable), '-NoProfile', '-NonInteractive', '-Command', _ACL_SCRIPT],
        env=dict(os.environ, MHPHOTOS_PRIVATE_PATH=str(path),
                 MHPHOTOS_PRIVATE_SET='1' if protect else '0'),
        capture_output=True, timeout=20, check=False,
    )
    if result.returncode:
        raise PermissionError('Owner-only Windows ACL required')


def reject_links(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise PermissionError('Symbolic links are not allowed')
        if (os.name == 'nt' and candidate.exists()
                and candidate.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise PermissionError('Reparse points are not allowed')


def validate_private(path: Path, *, directory: bool = False) -> None:
    reject_links(path)
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or (not directory and info.st_nlink != 1):
        raise PermissionError('Expected a private regular file or directory')
    if os.name == 'nt':
        windows_acl(path)
    elif info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
        raise PermissionError('Owner-only mode 700/600 required')


def private_directory(path: Path) -> None:
    reject_links(path)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        validate_private(path, directory=True)
        return
    if os.name == 'nt':
        windows_acl(path, protect=True)
    validate_private(path, directory=True)


def write_new_private(path: Path, data: bytes) -> None:
    """Parent must already be private; never replace an existing file."""
    validate_private(path.parent, directory=True)
    reject_links(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        if os.name == 'nt':
            windows_acl(path, protect=True)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    validate_private(path)
