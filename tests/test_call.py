"""`mhphotos-lightroom-mcp call ...`: the MHPhotos tools without an MCP client (cloud session, terminal)."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from mhphotos_lightroom_mcp import cli

from .fake_mhphotos import FakeMHPhotos


@pytest.fixture
def app(tmp_path):
    f = FakeMHPhotos()
    env = {
        **os.environ,
        "MHPHOTOS_INBOX_URL": f.url,
        "MHPHOTOS_ADMIN_TOKEN": "secret",
        "MHPHOTOS_CONFIG_DIR": str(tmp_path / "cfg"),
        "MHPHOTOS_ALLOW_INSTAGRAM": "1",
        "MHPHOTOS_ALLOW_PUBLISH": "1",
    }
    yield f, env
    f.stop()


def run(env, *args, cwd=None):
    res = subprocess.run(
        [sys.executable, "-m", "mhphotos_lightroom_mcp.server", "call", *args],
        env=env, capture_output=True, text=True, timeout=60, check=False, cwd=cwd,
    )
    return res.returncode, res.stdout, res.stderr


def when():
    return (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")


def test_call_reads_the_inbox(app):
    _, env = app
    code, out, _ = run(env, "mhphotos_inbox", '{"status": "wartet"}')
    data = json.loads(out)
    assert code == 0 and data["total"] == 3
    assert next(i for i in data["items"] if i["id"] == "lr-1")["suggestion"]["title"] == "Abendlicht am Aasee"


def test_call_saves_images_as_files(app, tmp_path):
    _, env = app
    out_dir = tmp_path / "bilder"
    code, out, _ = run(env, "mhphotos_inbox_images", '{"item_ids": ["lr-1", "lr-2"], "size": 400}', "--out", str(out_dir))
    data = json.loads(out)
    assert code == 0
    files = [d["image_file"] for d in data if "image_file" in d]
    assert len(files) == 2 and all(os.path.isfile(f) for f in files)
    with open(files[0], "rb") as fh:
        assert fh.read(2) == b"\xff\xd8"  # a JPEG a human or Claude can open
    assert data[0]["id"] == "lr-1"


def test_call_default_out_folder_is_relative_to_cwd(app, tmp_path):
    _, env = app
    code, _, _ = run(env, "mhphotos_inbox_contact_sheet", '{"item_ids": ["lr-1", "lr-2"]}', cwd=tmp_path)
    assert code == 0 and (tmp_path / "mhphotos-out" / "image-01.jpg").is_file()


def test_call_schedule_flow_dry_run_then_confirm(app):
    fake, env = app
    args = {"caption": "Moin", "scheduled_at": when(), "inbox_item_id": "lr-1"}
    code, out, _ = run(env, "mhphotos_schedule_instagram", json.dumps(args))
    assert code == 0 and json.loads(out)["dry_run"] is True and fake.social_uploads == []
    code, out, _ = run(env, "mhphotos_schedule_instagram", json.dumps({**args, "confirm": True}))
    assert code == 0 and json.loads(out)["applied"] is True and len(fake.social_uploads) == 1


def test_call_confirm_require_mode_refuses_because_nobody_can_be_asked(app):
    fake, env = app
    args = {"caption": "Moin", "scheduled_at": when(), "inbox_item_id": "lr-1", "confirm": True}
    _, out, _ = run({**env, "MHPHOTOS_CONFIRMATION": "require"}, "mhphotos_schedule_instagram", json.dumps(args))
    assert json.loads(out)["code"] == "approval_unavailable" and fake.social_uploads == []


def test_call_publish_goes_through_the_normal_gate(app):
    fake, env = app
    _, out, _ = run(env, "mhphotos_publish", '{"item_ids": ["lr-1"]}')
    assert json.loads(out)["dry_run"] is True and not any(c[0] == "PATCH" for c in fake.calls)
    _, out, _ = run(env, "mhphotos_publish", '{"item_ids": ["lr-1"], "confirm": true}')
    assert json.loads(out)["published"] == 1


def test_call_lightroom_tools_are_not_offered(app):
    _, env = app
    for tool in ("set_rating", "list_photos", "apply_preset", "undo_last_change", "os_system"):
        code, _, err = run(env, tool, "{}")
        assert code == 2 and "Unknown or unavailable tool" in err, tool


def test_call_photo_id_source_says_no_lightroom_instead_of_using_sample_data(app, tmp_path):
    _, env = app
    args = {"caption": "x", "scheduled_at": when(), "photo_id": "p4", "confirm": True}
    _, out, _ = run({**env, "MHPHOTOS_EXPORT_DIR": str(tmp_path / "e")}, "mhphotos_schedule_instagram", json.dumps(args))
    data = json.loads(out)
    assert data["code"] == "unavailable" and "not available in `call` mode" in data["error"]


def test_call_bad_input(app):
    _, env = app
    assert run(env, "mhphotos_inbox", "not json")[0] == 2
    assert run(env, "mhphotos_inbox", "[1]")[0] == 2
    assert run(env, "mhphotos_inbox", '{"bogus": 1}')[0] == 2
    assert run(env)[0] == 2
    assert run(env, "mhphotos_inbox", "--out")[0] == 2


def test_call_without_config_reports_disabled(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("MHPHOTOS_")}
    env["MHPHOTOS_CONFIG_DIR"] = str(tmp_path)
    code, out, _ = run(env, "mhphotos_inbox")
    assert code == 0 and json.loads(out)["code"] == "disabled"


def test_callable_tools_are_all_real_tools():
    import anyio
    from mhphotos_lightroom_mcp import server

    registered = {t.name for t in anyio.run(server.mcp.list_tools)}
    assert cli.callable_tools() <= registered
