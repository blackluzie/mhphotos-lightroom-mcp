"""The user is asked directly (MCP elicitation) before a confirmed change is applied."""

import json
import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


def _session(tmp_path, callback, **env):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mhphotos_lightroom_mcp.server"],
        env={
            **os.environ,
            "MHPHOTOS_ALLOW_WRITES": "1",
            "MHPHOTOS_CONFIG_DIR": str(tmp_path / "cfg"),
            **env,
        },
    )
    kwargs = {"elicitation_callback": callback} if callback else {}
    return stdio_client(params), kwargs


def run(tmp_path, callback, calls, **env):
    """Run [(tool, args), ...] against a fresh server; returns the parsed JSON results."""

    async def go():
        transport, kwargs = _session(tmp_path, callback, **env)
        out = []
        async with transport as (r, w), ClientSession(r, w, **kwargs) as s:
            await s.initialize()
            for name, args in calls:
                res = await s.call_tool(name, args)
                out.append(json.loads(res.content[0].text))
        return out

    return anyio.run(go)


SET_5 = ("set_rating", {"photo_ids": ["p2"], "rating": 5, "confirm": True})
READ_P2 = ("get_photo_metadata", {"photo_id": "p2"})


def audit_entries(tmp_path):
    f = tmp_path / "cfg" / "audit.log"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def test_user_accepts(tmp_path):
    asked = []

    async def accept(ctx, params):
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"approve": True})

    applied, meta = run(tmp_path, accept, [SET_5, READ_P2])
    assert applied["applied"] is True and meta["rating"] == 5
    assert len(asked) == 1 and "Bewertung auf 5 Sterne" in asked[0] and "IMG_0002.dng" in asked[0]
    assert audit_entries(tmp_path)[0]["approved_by"] == "user"


def test_user_declines(tmp_path):
    async def decline(ctx, params):
        return types.ElicitResult(action="decline")

    declined, meta = run(tmp_path, decline, [SET_5, READ_P2])
    assert declined["declined"] is True and meta["rating"] == 3  # nothing changed
    assert audit_entries(tmp_path) == []


def test_user_cancels(tmp_path):
    async def cancel(ctx, params):
        return types.ElicitResult(action="cancel")

    res, meta = run(tmp_path, cancel, [SET_5, READ_P2])
    assert res["declined"] is True and meta["rating"] == 3


def test_user_accepts_dialog_but_unticks_approve(tmp_path):
    async def unticked(ctx, params):
        return types.ElicitResult(action="accept", content={"approve": False})

    res, meta = run(tmp_path, unticked, [SET_5, READ_P2])
    assert res["declined"] is True and meta["rating"] == 3


def test_client_cannot_ask_default_falls_back_to_confirm_flag(tmp_path):
    res, meta = run(tmp_path, None, [SET_5, READ_P2])
    assert res["applied"] is True and meta["rating"] == 5
    assert audit_entries(tmp_path)[0]["approved_by"] == "flag (client cannot ask)"


def test_client_cannot_ask_require_mode_refuses(tmp_path):
    res, meta = run(tmp_path, None, [SET_5, READ_P2], MHPHOTOS_CONFIRMATION="require")
    assert res["code"] == "approval_unavailable" and meta["rating"] == 3
    assert audit_entries(tmp_path) == []


def test_flag_mode_never_asks(tmp_path):
    asked = []

    async def would_decline(ctx, params):
        asked.append(1)
        return types.ElicitResult(action="decline")

    res, meta = run(tmp_path, would_decline, [SET_5, READ_P2], MHPHOTOS_CONFIRMATION="flag")
    assert res["applied"] is True and meta["rating"] == 5 and asked == []


def test_dry_run_never_asks(tmp_path):
    asked = []

    async def cb(ctx, params):
        asked.append(1)
        return types.ElicitResult(action="accept", content={"approve": True})

    res, = run(tmp_path, cb, [("set_rating", {"photo_ids": ["p2"], "rating": 5})])
    assert res["dry_run"] is True and asked == []


def test_undo_also_asks(tmp_path):
    answers = iter(["accept", "decline"])

    async def cb(ctx, params):
        action = next(answers)
        content = {"approve": True} if action == "accept" else None
        return types.ElicitResult(action=action, content=content)

    _, undone, meta = run(
        tmp_path, cb, [SET_5, ("undo_last_change", {"confirm": True}), READ_P2]
    )
    assert undone["declined"] is True and meta["rating"] == 5  # the undo was declined


def test_publish_asks_before_going_public(tmp_path):
    from .fake_mhphotos import FakeMHPhotos

    app = FakeMHPhotos()
    asked = []

    async def decline(ctx, params):
        asked.append(params.message)
        return types.ElicitResult(action="decline")

    try:
        res, = run(
            tmp_path, decline,
            [("mhphotos_publish", {"item_ids": ["lr-1"], "confirm": True})],
            MHPHOTOS_INBOX_URL=app.url, MHPHOTOS_ADMIN_TOKEN="secret", MHPHOTOS_ALLOW_PUBLISH="1",
        )
        assert res["declined"] is True
        assert "ÖFFENTLICH" in asked[0] and "Abendlicht am Aasee" in asked[0]
        assert not any(c[0] == "PATCH" for c in app.calls)  # nothing reached the public site
    finally:
        app.stop()
