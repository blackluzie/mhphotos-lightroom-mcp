import asyncio

import pytest
from mhphotos_lightroom_mcp.oauth_http import validate_redirect

from .test_oauth_http import flow


@pytest.mark.parametrize('redirect', [
    'https://claude.ai/api/mcp/auth_callback',
    'https://claude.com/api/mcp/auth_callback',
    'http://127.0.0.1:45123/callback/client-id',
    'http://[::1]:45123/callback',
])
def test_full_oauth_with_other_clients(redirect):
    # Existing full protocol test: DCR, PKCE, consent, tools/call, refresh rotation,
    # replay, audience, cross-client isolation and wrong owner. GitHub identity is synthetic.
    asyncio.run(flow(redirect))


@pytest.mark.parametrize('uri', [
    'http://evil.example/callback', 'http://localhost:1234/callback',
    'http://127.0.0.1/callback', 'https://user:secret@client.example/callback',
    'https://*.example/callback', 'https://client.example/callback?next=evil',
    'https://client.example/callback#fragment', 'https://client.example/callback\n',
    'https://client.example;evil/callback', 'javascript:alert(1)',
])
def test_invalid_callbacks_are_rejected(uri):
    with pytest.raises(ValueError):
        validate_redirect(uri)
