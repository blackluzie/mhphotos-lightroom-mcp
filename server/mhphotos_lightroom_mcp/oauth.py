"""Single-owner OAuth provider. Credentials are ephemeral and never logged or persisted."""

import base64
import hashlib
import html
import logging
import os
import re
import secrets
import sqlite3
import stat
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)

SCOPE = "lightroom:read"
OWNER_ID = "56870555"  # Verified via https://api.github.com/users/blackluzie
COOKIE = "__Host-mhphotos-oauth"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def challenge(value):
    return (
        base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest())
        .decode()
        .rstrip("=")
    )


class Provider:
    def __init__(self, issuer, secret, redirects, client_db=None):
        self.issuer = issuer
        self.resource = issuer + "/mcp"
        self.secret = secret
        self.redirects = frozenset(redirects)
        self.rate = []
        self.clients = {}
        self.client_db = None
        if client_db:
            fd = os.open(client_db, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            os.close(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("Client database requires owner-only mode 600")
            self.client_db = sqlite3.connect(client_db)
            self.client_db.execute(
                "CREATE TABLE IF NOT EXISTS clients (id TEXT PRIMARY KEY, metadata TEXT NOT NULL)"
            )
            for key, metadata in self.client_db.execute(
                "SELECT id, metadata FROM clients"
            ):
                client = OAuthClientInformationFull.model_validate_json(metadata)
                if (
                    all(str(uri) in self.redirects for uri in client.redirect_uris)
                    and client.token_endpoint_auth_method == "none"
                ):
                    self.clients[key] = client
        self.pending = {}
        self.codes = {}
        self.access = {}
        self.refresh = {}
        self.used_refresh = {}

    def prune(self):
        now = time.time()
        for store in (self.pending, self.codes, self.access, self.refresh):
            for key, item in list(store.items()):
                expiry = item["expires"] if isinstance(item, dict) else item.expires_at
                if expiry <= now:
                    del store[key]
        for key, (_, expiry) in list(self.used_refresh.items()):
            if expiry <= now:
                del self.used_refresh[key]

    async def get_client(self, client_id):
        return self.clients.get(client_id)

    async def register_client(self, client_info):
        # Log only validated public callback addresses and fixed diagnostic labels.
        logger = logging.getLogger("mhphotos.oauth")
        for uri in client_info.redirect_uris or []:
            value = str(uri)
            if re.fullmatch(
                r"https://chatgpt\.com/(?:connector/oauth/[A-Za-z0-9_-]+|connector_platform_oauth_redirect)",
                value,
            ):
                logger.warning(
                    "OAuth registration: public ChatGPT callback %s; allowlisted=%s",
                    value,
                    value in self.redirects,
                )
        logger.warning(
            "OAuth registration checks: public_client=%s, scope_valid=%s, grants_valid=%s, response_valid=%s",
            client_info.token_endpoint_auth_method == "none",
            client_info.scope == SCOPE,
            set(client_info.grant_types) <= {"authorization_code", "refresh_token"},
            client_info.response_types == ["code"],
        )
        if (
            client_info.token_endpoint_auth_method != "none"
            or not client_info.redirect_uris
            or any(str(uri) not in self.redirects for uri in client_info.redirect_uris)
            or not set(client_info.grant_types)
            <= {"authorization_code", "refresh_token"}
            or client_info.response_types != ["code"]
            or client_info.scope != SCOPE
        ):
            raise RegistrationError(
                "invalid_client_metadata",
                "Only configured ChatGPT callbacks and public PKCE clients allowed",
            )
        if len(self.clients) >= 100:
            raise RegistrationError(
                "invalid_client_metadata",
                "Client limit reached; review local client registry",
            )
        client_info.client_secret = None
        self.clients[client_info.client_id] = client_info.model_copy(deep=True)
        if self.client_db:
            with self.client_db:
                self.client_db.execute(
                    "INSERT INTO clients VALUES (?, ?)",
                    (client_info.client_id, client_info.model_dump_json()),
                )

    async def authorize(self, client, params: AuthorizationParams):
        self.prune()
        if (
            params.resource != self.resource
            or params.scopes != [SCOPE]
            or not params.state
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge)
            or str(params.redirect_uri) not in self.redirects
        ):
            raise AuthorizeError(
                "invalid_request", "Resource, scope, state and S256 PKCE are required"
            )
        if len(self.pending) >= 100:
            raise AuthorizeError(
                "temporarily_unavailable", "Too many pending authorizations"
            )
        state = secrets.token_urlsafe(32)
        self.pending[digest(state)] = {
            "params": params,
            "client": client.client_id,
            "expires": time.time() + 300,
            "stage": "start",
        }
        return self.issuer + "/auth/start?" + urlencode({"state": state})

    def transaction(self, state, request, stage):
        item = self.pending.get(digest(state))
        cookie = request.cookies.get(COOKIE, "")
        reason = None
        if not item:
            reason = "session_missing_or_already_used"
        elif item["expires"] <= time.time():
            reason = "session_expired"
        elif item["stage"] != stage and not (
            stage == "consent" and item["stage"] == "completed"
        ):
            reason = "unexpected_login_stage"
        elif stage != "start" and not cookie:
            reason = "browser_cookie_missing"
        elif stage != "start" and not secrets.compare_digest(
            item["browser"], digest(cookie)
        ):
            reason = "browser_cookie_changed"
        self.prune()
        if reason:
            logging.getLogger("mhphotos.oauth").warning(
                "OAuth session rejected: %s", reason
            )
            return None
        return item

    async def github_identity(self, code, verifier):
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": "Ov23li6Iq0E92O5aK11b",
                    "client_secret": self.secret,
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": self.issuer + "/auth/github/callback",
                },
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            token = response.json().get("access_token")
            if not isinstance(token, str) or not token:
                raise ValueError("Identity exchange failed")
            response = await client.get(
                "https://api.github.com/user",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.github+json",
                },
            )
            response.raise_for_status()
            return str(response.json().get("id"))

    async def load_authorization_code(self, client, authorization_code):
        self.prune()
        value = self.codes.get(digest(authorization_code))
        return (
            value.model_copy(update={"code": authorization_code})
            if value and value.client_id == client.client_id
            else None
        )

    def issue(self, client_id, scopes, resource, subject, family=None, deadline=None):
        self.prune()
        if (
            len(self.access) >= 1000
            or len(self.refresh) >= 1000
            or len(self.used_refresh) >= 10000
        ):
            raise TokenError("invalid_grant", "Token limit reached")
        family = family or secrets.token_urlsafe(32)
        deadline = deadline or int(time.time()) + 86400
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.access[digest(access)] = AccessToken(
            token=family,
            client_id=client_id,
            scopes=scopes,
            expires_at=min(int(time.time()) + 900, deadline),
            resource=resource,
            subject=subject,
        )
        self.refresh[digest(refresh)] = RefreshToken(
            token=family,
            client_id=client_id,
            scopes=scopes,
            expires_at=deadline,
            resource=resource,
            subject=subject,
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=min(900, deadline - int(time.time())),
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        value = self.codes.pop(digest(authorization_code.code), None)
        if (
            not value
            or value.client_id != client.client_id
            or value.expires_at <= time.time()
        ):
            raise TokenError("invalid_grant", "Code expired or already used")
        return self.issue(client.client_id, value.scopes, value.resource, value.subject)

    async def load_access_token(self, token):
        self.prune()
        value = self.access.get(digest(token))
        return (
            value
            if value and value.subject == OWNER_ID and value.resource == self.resource
            else None
        )

    async def load_refresh_token(self, client, refresh_token):
        self.prune()
        key = digest(refresh_token)
        used = self.used_refresh.get(key)
        if used:
            self.revoke_family(used[0])
            return None
        value = self.refresh.get(key)
        if not value or value.client_id != client.client_id:
            return None
        return value.model_copy(update={"token": refresh_token})

    def revoke_family(self, family):
        for store in (self.access, self.refresh):
            for key, value in list(store.items()):
                if value.token == family:
                    del store[key]

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        key = digest(refresh_token.token)
        value = self.refresh.pop(key, None)
        if (
            not value
            or value.client_id != client.client_id
            or value.expires_at <= time.time()
        ):
            raise TokenError("invalid_grant", "Refresh token expired or already used")
        if (
            not set(scopes) <= set(value.scopes)
            or value.resource != self.resource
            or value.subject != OWNER_ID
        ):
            raise TokenError("invalid_scope", "Invalid authorization")
        self.used_refresh[key] = (value.token, value.expires_at)
        self.revoke_family(value.token)
        return self.issue(
            client.client_id,
            scopes,
            value.resource,
            value.subject,
            value.token,
            value.expires_at,
        )

    async def revoke_token(self, token):
        # SDK returns raw refresh tokens from load_refresh_token, access tokens carry family IDs.
        value = (
            self.refresh.get(digest(token.token))
            if isinstance(token, RefreshToken)
            else token
        )
        if value:
            self.revoke_family(value.token)


def attach_routes(mcp, provider):
    @mcp.custom_route("/auth/start", methods=["GET"])
    async def start(request):
        state = request.query_params.get("state", "")
        item = provider.transaction(state, request, "start")
        if not item:
            return PlainTextResponse("Invalid authorization", 400, headers=HEADERS)
        cookie, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        item.update(stage="github", browser=digest(cookie), verifier=verifier)
        response = RedirectResponse(
            "https://github.com/login/oauth/authorize?"
            + urlencode(
                {
                    "client_id": "Ov23li6Iq0E92O5aK11b",
                    "redirect_uri": provider.issuer + "/auth/github/callback",
                    "state": state,
                    "code_challenge": challenge(verifier),
                    "code_challenge_method": "S256",
                    "scope": "",
                    "allow_signup": "false",
                    "login": "blackluzie",
                }
            ),
            headers=HEADERS,
        )
        response.set_cookie(
            COOKIE,
            cookie,
            secure=True,
            httponly=True,
            samesite="lax",
            max_age=300,
            path="/",
        )
        return response

    @mcp.custom_route("/auth/github/callback", methods=["GET"])
    async def callback(request):
        state = request.query_params.get("state", "")
        item = provider.transaction(state, request, "github")
        if not item:
            return PlainTextResponse("Invalid authorization", 400, headers=HEADERS)
        item["stage"] = (
            "exchanging"  # Consume before network I/O, including failed callbacks.
        )
        try:
            code = request.query_params.get("code", "")
            if (
                not code
                or await provider.github_identity(code, item.pop("verifier"))
                != OWNER_ID
            ):
                raise ValueError("Owner required")
        except (httpx.HTTPError, ValueError, KeyError):
            provider.pending.pop(digest(state), None)
            return PlainTextResponse(
                "GitHub authorization denied", 403, headers=HEADERS
            )
        item.update(stage="consent", csrf=secrets.token_urlsafe(32))
        logging.getLogger("mhphotos.oauth").warning(
            "OAuth consent page issued: session_saved=%s",
            digest(state) in provider.pending,
        )
        target = html.escape(str(item["params"].redirect_uri), quote=True)
        return HTMLResponse(
            '<!doctype html><html lang="de"><meta charset="utf-8"><title>MHPhotos Freigabe</title>'
            "<h1>Lightroom-Lesezugriff freigeben</h1><p>Der Client darf Verbindungsstatus und Metadaten "
            "der ausdrücklich übermittelten Fotoauswahl lesen.</p><p>Rückleitung: "
            + target
            + "</p>"
            '<form method="post" action="/auth/consent"><input type="hidden" name="state" value="'
            + html.escape(state)
            + '"><input type="hidden" name="csrf" value="'
            + item["csrf"]
            + '"><button name="decision" value="allow">Freigeben</button>'
            '<button name="decision" value="deny">Ablehnen</button></form></html>',
            # no-referrer makes native browser form POSTs send Origin: null.
            # Send only the origin, never callback query parameters or codes.
            headers={
                **HEADERS,
                "Referrer-Policy": "strict-origin",
                # Browsers apply form-action to the cross-origin 302 redirect as well.
                # Only this service and the fixed ChatGPT HTTPS origin are permitted.
                "Content-Security-Policy": "default-src 'none'; form-action 'self' https://chatgpt.com; frame-ancestors 'none'",
            },
        )

    @mcp.custom_route("/auth/consent", methods=["POST"])
    async def consent(request):
        form = await request.form()
        state = str(form.get("state", ""))
        logging.getLogger("mhphotos.oauth").warning(
            "OAuth consent form: state_present=%s, state_format_valid=%s, csrf_present=%s, decision_present=%s, session_found=%s, pending_sessions=%s",
            bool(state),
            bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", state)),
            bool(form.get("csrf")),
            form.get("decision") in {"allow", "deny"},
            digest(state) in provider.pending,
            len(provider.pending),
        )
        item = provider.transaction(state, request, "consent")
        if (
            not item
            or request.headers.get("origin") != provider.issuer
            or not secrets.compare_digest(str(form.get("csrf", "")), item["csrf"])
        ):
            logging.getLogger("mhphotos.oauth").warning(
                "OAuth consent rejected: browser_transaction_valid=%s, origin_valid=%s, csrf_valid=%s",
                bool(item),
                request.headers.get("origin") == provider.issuer,
                bool(item)
                and secrets.compare_digest(str(form.get("csrf", "")), item["csrf"]),
            )
            return PlainTextResponse("Invalid consent", 400, headers=HEADERS)
        if item["stage"] == "completed":
            logging.getLogger("mhphotos.oauth").warning(
                "OAuth consent repeated: returning original redirect"
            )
            return RedirectResponse(item["redirect"], status_code=302, headers=HEADERS)
        params = item["params"]
        query = {"state": params.state}
        if form.get("decision") == "allow":
            code = secrets.token_urlsafe(32)
            provider.codes[digest(code)] = AuthorizationCode(
                code="",
                client_id=item["client"],
                scopes=params.scopes,
                expires_at=time.time() + 60,
                code_challenge=params.code_challenge,
                redirect_uri=params.redirect_uri,
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                resource=params.resource,
                subject=OWNER_ID,
            )
            query["code"] = code
        else:
            query["error"] = "access_denied"
        response = RedirectResponse(
            str(params.redirect_uri) + "?" + urlencode(query),
            status_code=302,
            headers=HEADERS,
        )
        # A retried/double-clicked POST returns the same one-use code, never a new grant.
        item.update(
            stage="completed",
            redirect=response.headers["location"],
            expires=time.time() + 60,
        )
        logging.getLogger("mhphotos.oauth").warning(
            "OAuth consent completed: decision=%s",
            "allow" if form.get("decision") == "allow" else "deny",
        )
        return response


class OAuthBoundary(BaseHTTPMiddleware):
    """Constrain public OAuth traffic and bind token requests to this resource."""

    def __init__(self, app, provider):
        super().__init__(app)
        self.provider = provider

    async def dispatch(self, request, call_next):
        if request.url.path != "/mcp":
            now = time.monotonic()
            self.provider.rate = [
                value for value in self.provider.rate if value > now - 60
            ]
            if len(self.provider.rate) >= 120:
                return PlainTextResponse(
                    "Try again later", 429, headers={**HEADERS, "Retry-After": "60"}
                )
            self.provider.rate.append(now)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 16384:
                    return PlainTextResponse("Request too large", 413, headers=HEADERS)
            request._body = bytes(body)
            if request.url.path == "/token" and request.method == "POST":
                from urllib.parse import parse_qs

                form = parse_qs(bytes(body).decode("utf-8", errors="replace"))
                logging.getLogger("mhphotos.oauth").warning(
                    "OAuth token request: resource_present=%s, resource_valid=%s, client_present=%s, code_present=%s, verifier_present=%s",
                    bool(form.get("resource")),
                    form.get("resource") == [self.provider.resource],
                    bool(form.get("client_id")),
                    bool(form.get("code")),
                    bool(form.get("code_verifier")),
                )
                if form.get("resource") != [self.provider.resource]:
                    logging.getLogger("mhphotos.oauth").warning(
                        "OAuth endpoint /token returned 400: invalid_target"
                    )
                    return JSONResponse(
                        {"error": "invalid_target"}, status_code=400, headers=HEADERS
                    )
        response = await call_next(request)
        if request.url.path in {
            "/register",
            "/token",
            "/mcp",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource/mcp",
        }:
            logging.getLogger("mhphotos.oauth").warning(
                "OAuth endpoint %s returned %s", request.url.path, response.status_code
            )
        if (
            request.url.path == "/.well-known/oauth-authorization-server"
            and response.status_code == 200
        ):
            import json

            data = json.loads(
                b"".join([chunk async for chunk in response.body_iterator])
            )
            data["token_endpoint_auth_methods_supported"] = ["none"]
            data["revocation_endpoint_auth_methods_supported"] = ["none"]
            return JSONResponse(data, headers=HEADERS)
        return response


class SecureHTTP(FastMCP):
    def streamable_http_app(self):
        app = super().streamable_http_app()
        provider = self._auth_server_provider
        app.add_middleware(OAuthBoundary, provider=provider)
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[urlsplit(provider.issuer).hostname, "127.0.0.1"],
        )
        return app


def create_http_server():
    issuer = os.environ.get("MHPHOTOS_PUBLIC_URL", "").rstrip("/")
    parsed = urlsplit(issuer)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.port
    ):
        raise ValueError(
            "MHPHOTOS_PUBLIC_URL must be an HTTPS origin without path, port or credentials"
        )
    redirects = os.environ.get("MHPHOTOS_CHATGPT_REDIRECT_URIS", "").split()
    if not redirects:
        raise ValueError(
            "Set exact ChatGPT callback URLs in MHPHOTOS_CHATGPT_REDIRECT_URIS"
        )
    for uri in redirects:
        url = urlsplit(uri)
        if (
            url.scheme != "https"
            or url.netloc != "chatgpt.com"
            or url.query
            or url.fragment
            or not (
                re.fullmatch(r"/connector/oauth/[A-Za-z0-9_-]+", url.path)
                or url.path == "/connector_platform_oauth_redirect"
            )
        ):
            raise ValueError("Only exact ChatGPT connector callback URLs are allowed")
    path = Path.home() / ".config/mhphotos-lightroom/github-client-secret"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise ValueError(
                "GitHub secret must be an owner-controlled regular file with mode 600"
            )
        secret = handle.read().strip()
    if not secret:
        raise ValueError("GitHub secret file is empty")
    parent_info = path.parent.stat()
    if (
        parent_info.st_uid != os.getuid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
        or path.parent.is_symlink()
    ):
        raise ValueError("OAuth config directory requires owner-only mode 700")
    provider = Provider(
        issuer, secret, redirects, path.parent / "oauth-clients.sqlite3"
    )
    mcp = SecureHTTP(
        "MHPhotos Lightroom MCP",
        host="127.0.0.1",
        port=8766,
        stateless_http=True,
        json_response=True,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=issuer,
            resource_server_url=provider.resource,
            validate_token_resource=True,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
        transport_security=TransportSecuritySettings(
            allowed_hosts=[parsed.hostname, "127.0.0.1:8766"], allowed_origins=[issuer]
        ),
    )
    attach_routes(mcp, provider)
    return mcp
