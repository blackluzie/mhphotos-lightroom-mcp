"""Stand-in for the Lua plugin: speaks the real bridge HTTP protocol, answers from a MockAdapter."""

import json
import threading
import urllib.error
import urllib.request

from mhphotos_lightroom_mcp.adapters import MockAdapter, NotFoundError


def _post(base, token, path, payload, timeout=15):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class FakePlugin:
    def __init__(self, bridge, backing=None):
        self.bridge = bridge
        self.backing = backing or MockAdapter()
        self.commands: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _loop(self):
        while not self._stop.is_set():
            try:
                msg = _post(self.bridge.url, self.bridge.token, "/v1/poll", {})
            except (urllib.error.URLError, OSError):
                continue
            cmd = msg.get("command")
            if not cmd:
                continue
            self.commands.append(cmd["name"])
            try:
                data = self._run(cmd["name"], cmd["args"])
                result = {"id": cmd["id"], "ok": True, "data": data}
            except NotFoundError as exc:
                result = {"id": cmd["id"], "ok": False, "error": f"not_found: {exc}"}
            _post(self.bridge.url, self.bridge.token, "/v1/result", result)

    def _run(self, name, a):
        b = self.backing
        if name == "list_collections":
            return {"collections": [c.summary() for c in b.list_collections()]}
        if name == "list_photos":
            photos, total = b.list_photos(a["collection_id"], a["limit"], a["offset"])
            return {"total": total, "photos": [p.metadata() for p in photos]}
        if name == "get_photo":
            return b.get_photo(a["photo_id"]).metadata()
        if name == "set_rating":
            return b.set_rating(a["photo_ids"], a["rating"])
        if name == "set_flag":
            return b.set_flag(a["photo_ids"], a["flag"])
        if name == "add_keywords":
            return b.add_keywords(a["photo_ids"], a["keywords"])
        if name == "export_photos":
            return b.export_photos(a["photo_ids"], a["dest_dir"])
        raise AssertionError(name)

