# User installation on macOS and Windows

Python 3.11 or newer and a complete, reviewed Git checkout are required. The
installer uses HTTPS PyPI for dependencies and installs the reviewed local
project (no remote script execution, administrator privileges or shell piping).
This is a source installer, not a signed native application distribution.

From the checkout, preview the plan (no writes):

macOS:
```sh
PYTHONPATH=server python3 -m mhphotos_lightroom_mcp.installer --name desktop-v1
```

Windows PowerShell:
```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'server'
py -3 -m mhphotos_lightroom_mcp.installer --name desktop-v1
```

Add `--install` to create the installation. No token entry is needed. Review the
checkout before running it, including local modifications. Existing installations
are never overwritten: choose a different name for an upgrade.

## What gets installed

Under `~/.mhphotos-lightroom/installations/<name>`:

- An isolated Python environment at its final path, with a non-editable package.
- A separate `MHPhotosLightroom.lrplugin`, with the exact Python executable path.
- A macOS launchd plist or a Windows per-user Task Scheduler XML file.
- `installation.json` and `READY` after verification. `FAILED` identifies an
  incomplete installation, which must not be activated.

The existing `~/.mhphotos-lightroom-token` is validated and reused byte for byte.
A fresh token is created exclusively and never printed. Existing insecure files
are refused rather than repaired silently. macOS uses owner-only permissions;
Windows uses the current user's SID and an owner-only ACL, with private directory
inheritance for transfer files. Symlinks, reparse points and hardlinked credentials
are refused. ACL failures abort installation before secret data is written.

The Windows helper uses a quoted executable and transfer filename. Paths with
command expansion/metacharacters (`%`, `!`, `&`, quotes, parentheses, etc.) are
refused. Spaces and Unicode are supported. Windows Documents redirection does
not determine the user's home directory.

## Explicit activation

Installation does **not** start, stop or register a service, rotate credentials,
change a tunnel, edit OAuth configuration, or replace the installed Lightroom
plugin. This makes it safe to prepare an upgrade while the existing bridge runs.
The generated service starts only the loopback bridge, not the public MCP server.

For a fresh installation, after checking `READY` and verifying that port 8765 is
free and no existing MHPhotos bridge service is registered:

- macOS: copy the generated plist into `~/Library/LaunchAgents/`, then use
  `launchctl bootstrap gui/$(id -u) <absolute-plist-path>`.
- Windows: import the generated XML in Task Scheduler as
  `MHPhotos Lightroom Bridge`, using the current account and **without**
  highest privileges. The generated task binds the logon trigger to this user's
  SID and uses an interactive token; no account password is stored.

The current account must be logged in. Do not register a second bridge alongside
a working bridge. An upgrade requires a separately scheduled service switch;
retain the old service configuration and plugin until the new transfer is verified.
No activation or service switch is performed by this installer.

In Lightroom Classic's Plug-in Manager, add the new plugin directory from the
plan when ready to switch. The original plugin and legacy macOS helper remain
usable. The new plugin explicitly transfers at most five selected photos through
a private local file; the Python helper reads the credential itself, sends only
the permitted metadata and deletes accepted transfer files. Ratings are read-only:
there is no catalog-write operation or rating-change tool.

For rollback, select the old plugin and restore the previous service configuration
at the scheduled switch. Do not delete the shared token or transfer directory.
A failed, unregistered installation directory may be removed after inspection.
Virtual environments contain absolute paths; recreate them rather than moving them.
See [Python venv documentation](https://docs.python.org/3/library/venv.html) and
[Windows ACL documentation](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.security/set-acl?view=powershell-5.1).

## Validation

CI runs all unit/integration tests and lint on macOS, Windows and Linux. Native
Windows tests check ACL creation, shared-ACL rejection, private transfer files and
fail-closed errors. An additional smoke test performs a real installation in an
isolated temporary user directory, without touching the actual home or port 8765.
Lightroom SDK execution and actual task registration still require manual checks
on each platform before release. In particular verify Windows `LrTasks.execute`
with spaces/Unicode in paths and Task Scheduler logon startup.
