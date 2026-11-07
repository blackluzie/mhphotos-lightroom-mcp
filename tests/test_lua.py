"""Run the Lua unit tests (Lua 5.1, same version as Lightroom). Skipped if lua is missing."""

import shutil
import subprocess
from pathlib import Path

import pytest

LUA = shutil.which("lua5.1") or shutil.which("lua") or shutil.which("luajit")
TESTS = Path(__file__).resolve().parent.parent / "plugin" / "tests"


@pytest.mark.skipif(LUA is None, reason="lua not installed")
@pytest.mark.parametrize("script", ["json_test.lua", "commands_test.lua", "autoexport_test.lua", "version_test.lua", "webhook_test.lua", "status_test.lua", "publish_test.lua", "loop_test.lua", "virtual_copy_test.lua", "keywords_test.lua"])
def test_lua(script):
    res = subprocess.run([LUA, script], check=False, cwd=TESTS, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
