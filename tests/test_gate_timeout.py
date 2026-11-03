import anyio
from mhphotos_lightroom_mcp import gate


class SilentClient:
    async def elicit(self, **kwargs):
        await anyio.sleep(3600)


def test_unanswered_question_fails_closed(monkeypatch):
    monkeypatch.setattr(gate, 'ELICIT_TIMEOUT_SECONDS', 0.1)
    monkeypatch.setenv(gate.MODE_ENV, 'ask')
    approved, how = anyio.run(gate.approve, SilentClient(), 'test')
    assert (approved, how) == (False, 'timeout')
    assert gate.refusal(how)['code'] == 'approval_timeout'
