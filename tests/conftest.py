import pytest
from mhphotos_lightroom_mcp import publishing, server
from mhphotos_lightroom_mcp.adapters import MockAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MHPHOTOS_ALLOW_WRITES", raising=False)
    monkeypatch.delenv("MHPHOTOS_EXPORT_DIR", raising=False)
    monkeypatch.delenv("MHPHOTOS_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("MHPHOTOS_ADMIN_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MHPHOTOS_INBOX_URL", raising=False)
    monkeypatch.delenv("MHPHOTOS_ALLOW_PUBLISH", raising=False)
    monkeypatch.delenv("MHPHOTOS_TOOLSETS", raising=False)
    publishing._keychain_cache.clear()
    publishing._keychain_cache["v"] = None  # never read a developer's real Keychain in tests
    monkeypatch.setenv("MHPHOTOS_CONFIG_DIR", str(tmp_path / "cfg"))
    server.set_adapter(MockAdapter())
