import asyncio
import re
from urllib.parse import parse_qs, urlsplit

import httpx
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.transport_security import TransportSecuritySettings
from mhphotos_lightroom_mcp.oauth import (
    OWNER_ID,
    SCOPE,
    Provider,
    SecureHTTP,
    attach_routes,
    challenge,
    digest,
)

ORIGIN = "https://photos.example"
REDIRECT = "https://chatgpt.com/connector/oauth/test"


def test_full_http_oauth():
    asyncio.run(flow())


async def flow():
    provider = Provider(ORIGIN, "test-only", [REDIRECT])
    server = SecureHTTP(
        "test",
        stateless_http=True,
        json_response=True,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=ORIGIN,
            resource_server_url=provider.resource,
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        transport_security=TransportSecuritySettings(
            allowed_hosts=["photos.example"], allowed_origins=[ORIGIN]
        ),
    )
    attach_routes(server, provider)
    server.add_tool(lambda: {"ok": True}, name="status")
    app = server.streamable_http_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as http,
    ):
        for method in ("GET", "POST", "DELETE"):
            response = await http.request(method, "/mcp")
            assert response.status_code == 401
            assert "resource_metadata=" in response.headers["www-authenticate"]
        assert (
            await http.post("/mcp", headers={"Authorization": "Bearer invalid"})
        ).status_code == 401
        metadata = (await http.get("/.well-known/oauth-authorization-server")).json()
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert metadata["token_endpoint_auth_methods_supported"] == ["none"]
        resource = (await http.get("/.well-known/oauth-protected-resource/mcp")).json()
        assert resource["resource"] == provider.resource
        assert resource["authorization_servers"] == [metadata["issuer"]]
        body = {
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
        bad = await http.post(
            "/register",
            json={**body, "redirect_uris": ["https://evil.example/callback"]},
        )
        assert bad.status_code == 400
        registered = await http.post("/register", json=body)
        assert registered.status_code == 201, registered.text
        client_id = registered.json()["client_id"]
        verifier = "a" * 64
        params = {
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "state": "chatgpt-state",
            "scope": SCOPE,
            "resource": provider.resource,
            "code_challenge": challenge(verifier),
            "code_challenge_method": "S256",
        }
        bad = await http.get(
            "/authorize", params={**params, "code_challenge_method": "plain"}
        )
        assert "code=" not in bad.headers.get("location", "")
        bad = await http.get(
            "/authorize", params={**params, "resource": "https://evil.example"}
        )
        assert "error=" in bad.headers["location"]
        auth = await http.get("/authorize", params=params)
        start = await http.get(auth.headers["location"])
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        assert "code_challenge_method=S256" in start.headers["location"]
        assert (
            "Secure" in start.headers["set-cookie"]
            and "HttpOnly" in start.headers["set-cookie"]
        )

        async def identity(code, verifier):
            return OWNER_ID

        provider.github_identity = identity
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as other:
            assert (
                await other.get(
                    "/auth/github/callback", params={"state": state, "code": "test"}
                )
            ).status_code == 400
        consent = await http.get(
            "/auth/github/callback", params={"state": state, "code": "test"}
        )
        assert consent.status_code == 200
        assert consent.headers["referrer-policy"] == "strict-origin"
        assert (
            "form-action 'self' https://chatgpt.com;"
            in consent.headers["content-security-policy"]
        )
        assert "default-src 'none'" in consent.headers["content-security-policy"]
        assert (
            await http.get(
                "/auth/github/callback", params={"state": state, "code": "test"}
            )
        ).status_code == 400
        csrf = re.search('name="csrf" value="([^"]+)"', consent.text)[1]
        form_state = re.search('name="state" value="([^"]+)"', consent.text)[1]
        assert form_state == state
        data = {"state": form_state, "csrf": csrf, "decision": "allow"}
        assert (await http.post("/auth/consent", data=data)).status_code == 400
        for bad_origin in ("null", "https://evil.example"):
            assert (
                await http.post(
                    "/auth/consent", data=data, headers={"Origin": bad_origin}
                )
            ).status_code == 400
        assert (
            await http.post(
                "/auth/consent",
                data={**data, "csrf": "wrong"},
                headers={"Origin": ORIGIN},
            )
        ).status_code == 400
        allowed = await http.post(
            "/auth/consent", data=data, headers={"Origin": ORIGIN}
        )
        repeated = await http.post(
            "/auth/consent", data=data, headers={"Origin": ORIGIN}
        )
        assert repeated.status_code == 302
        assert repeated.headers["location"] == allowed.headers["location"]
        assert len(provider.codes) == 1
        assert (
            await http.post(
                "/auth/consent",
                data={**data, "csrf": "wrong"},
                headers={"Origin": ORIGIN},
            )
        ).status_code == 400
        query = parse_qs(urlsplit(allowed.headers["location"]).query)
        assert query["state"] == ["chatgpt-state"]
        code = query["code"][0]
        exchange = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
            "resource": provider.resource,
        }
        assert (
            await http.post("/token", data={**exchange, "code_verifier": "b" * 64})
        ).status_code == 400
        assert (
            await http.post(
                "/token", data={**exchange, "redirect_uri": REDIRECT + "evil"}
            )
        ).status_code == 400
        assert (
            await http.post(
                "/token", data={**exchange, "resource": "https://wrong.example"}
            )
        ).status_code == 400
        tokens = await http.post("/token", data=exchange)
        assert tokens.status_code == 200, tokens.text
        tokens = tokens.json()
        assert (await http.post("/token", data=exchange)).status_code == 400
        token = tokens["access_token"]
        assert token not in repr(provider.access)
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
        }
        response = await http.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == 200, response.text
        called = await http.post(
            "/mcp",
            headers={**headers, "MCP-Protocol-Version": "2025-11-25"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            },
        )
        assert called.status_code == 200 and "result" in called.json(), called.text
        assert not called.json()["result"].get("isError")
        refresh = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": tokens["refresh_token"],
            "resource": provider.resource,
        }
        rotated = await http.post("/token", data=refresh)
        assert rotated.status_code == 200, rotated.text
        assert await provider.load_access_token(token) is None
        assert (await http.post("/token", data=refresh)).status_code == 400
        assert await provider.load_access_token(rotated.json()["access_token"]) is None
        # A different authenticated GitHub user receives no connector code.
        auth = await http.get("/authorize", params=params)
        start = await http.get(auth.headers["location"])
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]

        async def wrong_identity(code, verifier):
            return "123"

        provider.github_identity = wrong_identity
        assert (
            await http.get(
                "/auth/github/callback", params={"state": state, "code": "test"}
            )
        ).status_code == 403
        # Expiration and audience validation.
        token = provider.issue(
            client_id, [SCOPE], provider.resource, OWNER_ID
        ).access_token
        provider.access[digest(token)].resource = "https://wrong.example"
        assert await provider.load_access_token(token) is None
        provider.access[digest(token)].expires_at = 0
        assert await provider.load_access_token(token) is None


def test_fail_closed_configuration(monkeypatch, tmp_path):
    import pytest
    from mhphotos_lightroom_mcp.oauth import create_http_server

    monkeypatch.setenv("MHPHOTOS_PUBLIC_URL", ORIGIN)
    monkeypatch.delenv("MHPHOTOS_CHATGPT_REDIRECT_URIS", raising=False)
    with pytest.raises(ValueError, match="callback"):
        create_http_server()
    monkeypatch.setenv("MHPHOTOS_CHATGPT_REDIRECT_URIS", REDIRECT)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = tmp_path / ".config/mhphotos-lightroom/github-client-secret"
    path.parent.mkdir(parents=True, mode=0o700)
    path.write_text("synthetic-test-secret")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="mode 600"):
        create_http_server()
    path.chmod(0o600)
    server = create_http_server()
    assert server.settings.host == "127.0.0.1"
    assert server.settings.auth.validate_token_resource is True
    assert server.settings.auth.required_scopes == [SCOPE]
    monkeypatch.setenv("MHPHOTOS_PUBLIC_URL", "http://photos.example")
    with pytest.raises(ValueError, match="HTTPS"):
        create_http_server()


def test_revocation_and_client_binding():
    asyncio.run(revocation())


async def revocation():
    from mcp.shared.auth import OAuthClientInformationFull

    provider = Provider(ORIGIN, "synthetic", [REDIRECT])
    client = OAuthClientInformationFull(
        client_id="client",
        redirect_uris=[REDIRECT],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        scope=SCOPE,
    )
    other = client.model_copy(update={"client_id": "other"})
    tokens = provider.issue("client", [SCOPE], provider.resource, OWNER_ID)
    assert await provider.load_refresh_token(other, tokens.refresh_token) is None
    refresh = await provider.load_refresh_token(client, tokens.refresh_token)
    await provider.revoke_token(refresh)
    assert await provider.load_access_token(tokens.access_token) is None
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None
    tokens = provider.issue("client", [SCOPE], provider.resource, OWNER_ID)
    access = await provider.load_access_token(tokens.access_token)
    await provider.revoke_token(access)
    assert await provider.load_refresh_token(client, tokens.refresh_token) is None


def test_public_clients_survive_restart(tmp_path):
    asyncio.run(persist_clients(tmp_path))


async def persist_clients(tmp_path):
    from mcp.shared.auth import OAuthClientInformationFull

    path = tmp_path / "clients.sqlite3"
    first = Provider(ORIGIN, "synthetic", [REDIRECT], path)
    client = OAuthClientInformationFull(
        client_id="saved-client",
        redirect_uris=[REDIRECT],
        token_endpoint_auth_method="none",
        grant_types=["authorization_code", "refresh_token"],
        scope=SCOPE,
    )
    await first.register_client(client)
    tokens = first.issue(client.client_id, [SCOPE], first.resource, OWNER_ID)
    second = Provider(ORIGIN, "synthetic", [REDIRECT], path)
    assert (await second.get_client("saved-client")).client_secret is None
    assert await second.load_access_token(tokens.access_token) is None
    assert await second.load_refresh_token(client, tokens.refresh_token) is None
    removed = Provider(ORIGIN, "synthetic", [], path)
    assert await removed.get_client("saved-client") is None
    assert path.stat().st_mode & 0o777 == 0o600


def test_http_tool_descriptors():
    from mcp.server.fastmcp import FastMCP
    from mhphotos_lightroom_mcp.server import register_http_tools

    server = FastMCP("descriptor-test")
    register_http_tools(server)
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {
        "connection_status",
        "selected_photo_metadata",
    }
    for tool in tools:
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
        assert tool.meta["securitySchemes"] == [{"type": "oauth2", "scopes": [SCOPE]}]
