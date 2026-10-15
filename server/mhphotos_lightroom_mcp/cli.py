"""Helper commands: ``doctor`` (setup check) and ``install-plugin`` (copy the Lua plugin)."""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from . import bridge, gate, permissions, publishing

OK, WARN, FAIL = "✓", "!", "✗"


def plugin_source() -> Path | None:
    """The plugin folder shipped inside the package, or the one in a source checkout."""
    here = Path(__file__).resolve().parent
    for candidate in (
        here / "plugin" / "mhphotos.lrdevplugin",
        here.parent.parent / "plugin" / "mhphotos.lrdevplugin",
    ):
        if (candidate / "Info.lua").is_file():
            return candidate
    return None


def install_plugin(dest_root: str | None = None) -> tuple[int, str]:
    src = plugin_source()
    if src is None:
        return 1, "Plugin files not found (broken install?)."
    root = Path(dest_root).expanduser() if dest_root else Path.home() / "Documents" / "MHPhotos-Lightroom-Plugin"
    target = root / "mhphotos.lrdevplugin"
    if target.exists():
        if not (target / "Info.lua").is_file():
            return 1, f"{target} exists but is not a Lightroom plugin folder. Choose another destination."
        shutil.rmtree(target)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, target, ignore=shutil.ignore_patterns("*.pyc", "__pycache__"))
    return 0, (
        f"Plugin copied to {target}\n"
        "Next: Lightroom Classic > File > Plug-in Manager > Add > select that folder.\n"
        "Then: Library > Plug-in Extras > 'MHPhotos MCP: Selbsttest (nur lesen)'."
    )


def _health(url: str, token: str) -> dict | None:
    req = urllib.request.Request(url.rstrip("/") + "/v1/health", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _app_status(code: int, host: str) -> str:
    if code == 200:
        return f"reachable, token accepted ({host})"
    if code in (401, 403):
        return f"token rejected (HTTP {code})"
    return "not reachable" if code == 0 else f"HTTP {code}"


def doctor() -> tuple[int, list[tuple[str, str, str]]]:
    rows: list[tuple[str, str, str]] = []

    def add(status: str, label: str, detail: str) -> None:
        rows.append((status, label, detail))

    try:
        add(OK, "mhphotos-lightroom-mcp", version("mhphotos-lightroom-mcp"))
    except PackageNotFoundError:
        add(WARN, "mhphotos-lightroom-mcp", "version unknown (running from a source checkout)")
    add(OK, "adapter", os.environ.get("MHPHOTOS_ADAPTER", "mock") + (
        "" if os.environ.get("MHPHOTOS_ADAPTER") else "  (set MHPHOTOS_ADAPTER=bridge for real Lightroom)"))

    # plugin files
    src = plugin_source()
    add(OK if src else FAIL, "plugin files", str(src) if src else "not found")

    # bridge.json and a running server
    cfg_file = bridge.config_dir() / "bridge.json"
    if not cfg_file.is_file():
        add(WARN, "bridge.json", f"{cfg_file} not found: start the MCP server once with MHPHOTOS_ADAPTER=bridge")
    else:
        mode = cfg_file.stat().st_mode & 0o777
        add(OK if mode & 0o077 == 0 else WARN, "bridge.json permissions", oct(mode) + ("" if mode & 0o077 == 0 else "  (should be 600)"))
        try:
            cfg = json.loads(cfg_file.read_text())
            health = _health(cfg["url"], cfg["token"])
        except (OSError, ValueError, KeyError):
            health = None
        if health is None:
            add(WARN, "MCP server", "not running (it starts with your MCP client) - nothing to check live")
        else:
            add(OK, "MCP server", "running, bridge reachable")
            add(OK if health.get("plugin_connected") else WARN, "Lightroom plugin",
                "connected" if health.get("plugin_connected") else "not connected: is Lightroom running with the plugin enabled?")

    # permissions (names only, never values)
    add(OK, "edits (MHPHOTOS_ALLOW_WRITES)", "enabled" if permissions.writes_enabled() else "off (read-only)")
    export = permissions.export_dir()
    if os.environ.get(permissions.EXPORT_DIR_ENV) and export is None:
        add(FAIL, "export folder", "MHPHOTOS_EXPORT_DIR must be an absolute path")
    elif export:
        add(OK if Path(export).exists() or Path(export).parent.exists() else WARN, "export folder", export)
    else:
        add(OK, "export folder", "not set (export disabled)")
    add(OK, "publish (MHPHOTOS_ALLOW_PUBLISH)", "enabled" if publishing.publishing_enabled() else "off")
    add(OK, "Instagram scheduling (MHPHOTOS_ALLOW_INSTAGRAM)", "enabled" if publishing.instagram_enabled() else "off")
    add(OK, "confirmation (MHPHOTOS_CONFIRMATION)", {
        "ask": "ask (you are asked when your client can; otherwise confirm=true)",
        "require": "require (refuses when your client cannot ask)",
        "flag": "flag (confirm=true is enough, you are never asked)",
    }[gate.mode()])
    add(OK, "toolsets (MHPHOTOS_TOOLSETS)", os.environ.get("MHPHOTOS_TOOLSETS") or "all")

    # MHPhotos app
    token, source = publishing.resolve_token()
    url = os.environ.get(publishing.URL_ENV, "").strip()
    if not url:
        add(OK, "MHPhotos app", f"{publishing.URL_ENV} not set (MHPhotos tools disabled)")
    elif not token:
        add(FAIL, "MHPhotos admin token", f"{source}. Store it in the Keychain: security add-generic-password -s {publishing.KEYCHAIN_SERVICE} -a \"$USER\" -w")
    else:
        add(OK, "MHPhotos admin token", f"found in {source}")
        cfg2 = publishing.inbox_config()
        if cfg2 is None:
            add(FAIL, "MHPhotos app", f"{publishing.URL_ENV} must be an https URL")
        else:
            code, _ = publishing.galleries(cfg2)
            add(OK if code == 200 else FAIL, "MHPhotos app", _app_status(code, cfg2.host))

    failed = any(r[0] == FAIL for r in rows)
    return (1 if failed else 0), rows


def run(argv: list[str]) -> int:
    cmd = argv[0]
    if cmd == "doctor":
        code, rows = doctor()
        for status, label, detail in rows:
            print(f"{status} {label}: {detail}")
        return code
    if cmd == "install-plugin":
        code, message = install_plugin(argv[1] if len(argv) > 1 else None)
        print(message)
        return code
    print("Usage: mhphotos-lightroom-mcp [doctor | install-plugin [DEST_FOLDER]]\n"
          "Without arguments the MCP server starts (stdio).")
    return 0 if cmd in {"-h", "--help", "help"} else 2
