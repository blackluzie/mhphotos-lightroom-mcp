import pytest
from mhphotos_lightroom_mcp import server
from mhphotos_lightroom_mcp.adapters import MockAdapter


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MHPHOTOS_ALLOW_WRITES", raising=False)
    monkeypatch.delenv("MHPHOTOS_EXPORT_DIR", raising=False)
    monkeypatch.setenv("MHPHOTOS_CONFIG_DIR", str(tmp_path / "cfg"))
    server.set_adapter(MockAdapter())
