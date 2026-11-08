"""Single-owner OAuth provider. Credentials are never logged.

Since 0.1.28 issued tokens survive a server restart: with a client database the provider keeps them in it, but
only as SHA-256 digests (the raw access and refresh tokens are never written anywhere), so the file does not
hand out a usable credential. A login lasts MHPHOTOS_OAUTH_SESSION_DAYS (default 30) after the client's last
token refresh; before, every restart and every 24 hours forced the user to reconnect the connector.
"""

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
COOKIE = "__Host-mhphotos-oauth"
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
}


def validate_redirect(uri):
    """Explicit HTTPS callbacks or an exact native-loopback callback; no patterns."""
    url = urlsplit(uri)
    try:
        port = url.port
    except ValueError as error:
        raise ValueError('Invalid callback port') from error
    if (not url.hostname or url.username or url.password or url.query or url.fragment
            or not url.path.startswith('/') or any(c.isspace() for c in uri)
            or any(c in uri for c in ('*', '<', '>', "'", '"', '\\', '%'))):
        raise ValueError('Callback must be an exact URL without credentials, query, fragment or patterns')
    if url.scheme == 'https':
        if not re.fullmatch(r'[A-Za-z0-9.-]+', url.hostname):
            raise ValueError('Invalid HTTPS callback hostname')
        if url.hostname in {'localhost', '127.0.0.1', '::1'}:
            raise ValueError('Native loopback callbacks must use HTTP with an explicit port')
    elif not (url.scheme == 'http' and url.hostname in {'127.0.0.1', '::1'} and port):
        raise ValueError('Callback requires HTTPS or an HTTP loopback IP with an explicit port')
    return uri


def callback_origin(uri):
    validate_redirect(uri)
    url = urlsplit(uri)
    return url.scheme + '://' + url.netloc


def github_settings(client_id, owner_id, login=None):
    """Validate the GitHub OAuth app and the single permitted owner of this deployment.

    owner_id is the numeric GitHub account id (stable across renames); login is only a
    sign-in hint for the GitHub page and grants nothing.
    """
    client_id, owner_id, login = str(client_id or ''), str(owner_id or ''), login or None
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", client_id):
        raise ValueError("Set the GitHub OAuth app client id (github_client_id)")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", owner_id):
        raise ValueError("Set the numeric GitHub account id of the owner (github_owner_id)")
    if login is not None and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", login):
        raise ValueError("Invalid GitHub login (github_login)")
    return {"client_id": client_id, "owner_id": owner_id, "login": login}


def consent_text():
    """What the consent page grants; it names catalog edits only when the operator enabled them."""
    text = _consent_base()
    if os.environ.get("MHPHOTOS_HTTP_INSTAGRAM") == "1":
        text += ("<p><strong>Instagram:</strong> Der Client darf au&szlig;erdem nach Best&auml;tigung Beitr&auml;ge in die "
                 "MHPhotos-Warteschlange einplanen, die dort zur geplanten Zeit automatisch auf Instagram erscheinen, "
                 "und geplante Beitr&auml;ge wieder entfernen. Ebenso darf er nach Best&auml;tigung Kommentare unter "
                 "den eigenen Beitr&auml;gen &ouml;ffentlich beantworten und liken.</p>")
    return text


def _consent_base():
    if os.environ.get("MHPHOTOS_HTTP_WRITES") == "1":
        return ("<h1>Lightroom-Zugriff mit Bearbeitung freigeben</h1><p>Der Client darf Sammlungen, Foto-Metadaten (auch Dateipfade f&uuml;r die Katalog-Pr&uuml;fung), "
                "Vorschauen und Entwicklungseinstellungen lesen, JPEG-Dateien in den festgelegten lokalen Exportordner "
                "schreiben und im Katalog Sterne, Markierungen (auch &bdquo;Abgelehnt&ldquo;), Farben, Titel, Stichw&ouml;rter, "
                "Sammlungen, virtuelle Kopien, Entwicklungseinstellungen und Presets &auml;ndern bzw. anlegen. "
                "Keine L&ouml;schungen und keine Ver&ouml;ffentlichungen.</p>")
    return ("<h1>Lightroom-Lesezugriff und lokalen Export freigeben</h1><p>Der Client darf Sammlungen, Foto-Metadaten (auch Dateipfade f&uuml;r die Katalog-Pr&uuml;fung) und "
            "Vorschauen lesen sowie nach Best&auml;tigung JPEG-Dateien in den festgelegten lokalen Exportordner schreiben. "
            "Keine Katalog&auml;nderungen oder Ver&ouml;ffentlichungen.</p>")


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def challenge(value):
    return (
        base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest())
        .decode()
        .rstrip("=")
    )


DEFAULT_SESSION_DAYS = 30
MAX_SESSION_DAYS = 90


def session_seconds(days=None):
    """Login lifetime after the last refresh, from MHPHOTOS_OAUTH_SESSION_DAYS (1-90 days, default 30)."""
    if days is None:
        raw = os.environ.get("MHPHOTOS_OAUTH_SESSION_DAYS", "").strip()
        try:
            days = float(raw) if raw else DEFAULT_SESSION_DAYS
        except ValueError:
            raise ValueError("MHPHOTOS_OAUTH_SESSION_DAYS must be a number of days") from None
    days = max(1.0, min(float(days), MAX_SESSION_DAYS))
    return int(days * 86400)


class Provider:
    def __init__(self, issuer, secret, redirects, client_db=None, *, github, session_days=None):
        self.github = github_settings(**github)
        self.owner_id = self.github["owner_id"]
        self.issuer = issuer
        self.resource = issuer + "/mcp"
        self.secret = secret
        self.redirects = frozenset(validate_redirect(str(uri)) for uri in redirects)
        self.rate = []
        self.session = session_seconds(session_days)
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
        self._load_tokens()

    # --- persisted tokens (digests only) ---
    def _load_tokens(self):
        if not self.client_db:
            return
        with self.client_db:
            self.client_db.execute(
                "CREATE TABLE IF NOT EXISTS tokens (kind TEXT NOT NULL, digest TEXT PRIMARY KEY, "
                "family TEXT NOT NULL, data TEXT NOT NULL, expires REAL NOT NULL)"
            )
            self.client_db.execute("DELETE FROM tokens WHERE expires <= ?", (time.time(),))
        rows = self.client_db.execute("SELECT kind, digest, family, data, expires FROM tokens").fetchall()
        for kind, key, family, data, expires in rows:
            if kind == "used":
                self.used_refresh[key] = (family, expires)
                continue
            model = AccessToken if kind == "access" else RefreshToken
            value = model.model_validate_json(data)
            # a token of a removed client or for another owner/resource is dropped, not loaded
            if (
                value.client_id not in self.clients
                or value.subject != self.owner_id
                or value.resource != self.resource
            ):
                self._forget(key)
                continue
            (self.access if kind == "access" else self.refresh)[key] = value

    def _save(self, kind, key, value, family=None, expires=None):
        if not self.client_db:
            return
        data = "{}" if kind == "used" else value.model_dump_json()
        with self.client_db:
            self.client_db.execute(
                "INSERT OR REPLACE INTO tokens VALUES (?, ?, ?, ?, ?)",
                (
                    kind,
                    key,
                    family if family is not None else value.token,
                    data,
                    expires if expires is not None else value.expires_at,
                ),
            )

    def _forget(self, key):
        if self.client_db:
            with self.client_db:
                self.client_db.execute("DELETE FROM tokens WHERE digest = ?", (key,))

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
        if self.client_db:
            with self.client_db:
                self.client_db.execute("DELETE FROM tokens WHERE expires <= ?", (now,))

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
                "Only configured exact callbacks and public PKCE clients allowed",
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
                    "client_id": self.github["client_id"],
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
        deadline = deadline or int(time.time()) + self.session
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        access_key, refresh_key = digest(access), digest(refresh)
        self.access[access_key] = AccessToken(
            token=family,
            client_id=client_id,
            scopes=scopes,
            expires_at=min(int(time.time()) + 900, deadline),
            resource=resource,
            subject=subject,
        )
        self.refresh[refresh_key] = RefreshToken(
            token=family,
            client_id=client_id,
            scopes=scopes,
            expires_at=deadline,
            resource=resource,
            subject=subject,
        )
        self._save("access", access_key, self.access[access_key])
        self._save("refresh", refresh_key, self.refresh[refresh_key])
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
            if value and value.subject == self.owner_id and value.resource == self.resource
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
        if self.client_db:
            with self.client_db:
                self.client_db.execute(
                    "DELETE FROM tokens WHERE family = ? AND kind != 'used'", (family,)
                )

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
            or value.subject != self.owner_id
        ):
            raise TokenError("invalid_scope", "Invalid authorization")
        self.used_refresh[key] = (value.token, value.expires_at)
        self._save("used", key, None, family=value.token, expires=value.expires_at)
        self.revoke_family(value.token)
        # sliding login: every refresh extends it to `session` from now, so a connector in regular use
        # never has to be reconnected; one unused for longer than that does
        return self.issue(
            client.client_id,
            scopes,
            value.resource,
            value.subject,
            value.token,
            int(time.time()) + self.session,
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
        query = {
            "client_id": provider.github["client_id"],
            "redirect_uri": provider.issuer + "/auth/github/callback",
            "state": state,
            "code_challenge": challenge(verifier),
            "code_challenge_method": "S256",
            "scope": "",
            "allow_signup": "false",
        }
        if provider.github["login"]:
            query["login"] = provider.github["login"]
        response = RedirectResponse(
            "https://github.com/login/oauth/authorize?" + urlencode(query),
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
                != provider.owner_id
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
            + consent_text()
            + "<p>Rückleitung: "
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
                # Only this service and the validated callback origin are permitted.
                "Content-Security-Policy": "default-src 'none'; form-action 'self' " + callback_origin(str(item["params"].redirect_uri)) + "; frame-ancestors 'none'",
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
                subject=provider.owner_id,
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
    redirects = os.environ.get("MHPHOTOS_REDIRECT_URIS", os.environ.get("MHPHOTOS_CHATGPT_REDIRECT_URIS", "")).split()
    if not redirects:
        raise ValueError("Set exact callback URLs in MHPHOTOS_REDIRECT_URIS")
    for uri in redirects:
        validate_redirect(uri)
    github = github_settings(
        os.environ.get("MHPHOTOS_GITHUB_CLIENT_ID"),
        os.environ.get("MHPHOTOS_GITHUB_OWNER_ID"),
        os.environ.get("MHPHOTOS_GITHUB_LOGIN") or None,
    )
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
        issuer, secret, redirects, path.parent / "oauth-clients.sqlite3", github=github
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
