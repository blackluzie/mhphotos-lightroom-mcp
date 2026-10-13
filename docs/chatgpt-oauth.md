# ChatGPT-Anbindung mit GitHub-OAuth

Implementiert für MCP SDK 1.30.0. Claude Desktop verwendet weiterhin den unveränderten stdio-Server. Der HTTP-Server bindet ausschließlich an 127.0.0.1:8766; die Bridge auf 8765 darf niemals veröffentlicht werden.

## Sicherheitsmodell

- ChatGPT verwendet Dynamic Client Registration (DCR), öffentliche Clients (`none`) und Authorization Code + S256 PKCE. CIMD wird nicht angekündigt. Die Client-ID der GitHub-App gehört ausschließlich zur GitHub-Anmeldung, nicht ins ChatGPT-Client-ID-Feld.
- Nur die exakt konfigurierte ChatGPT-Rückleitungsadresse wird registriert. Sie ist verbindungsspezifisch und muss aus der ChatGPT-Verwaltungsseite übernommen werden. Aktuell dokumentierte Adressen sind `https://chatgpt.com/connector/oauth/{callback_id}` bzw. für ältere Verbindungen `https://chatgpt.com/connector_platform_oauth_redirect`. Keine Platzhalter und keine Wildcards verwenden.
- GitHub `/user` muss die numerische ID **56870555** liefern. Der vorgeschlagene Loginname allein autorisiert niemanden. GitHub PKCE, einmaliger State und ein Secure/HttpOnly/SameSite=Lax-Browsercookie schützen den zweiten OAuth-Fluss.
- Vor Ausgabe eines MCP-Codes ist eine ausdrückliche Zustimmung mit CSRF-Schutz erforderlich. Berechtigt sind nur Status, Metadaten der ausdrücklich übermittelten Auswahl und ausdrücklich übermittelte Sammlungsübersichten (`lightroom:read`). Sammlungsübersichten enthalten ausschließlich begrenzte Namen und Hierarchie, keine Fotos oder Regeln.
- MCP-Zugriff benötigt bei jeder Anfrage ein gültiges, nicht abgelaufenes Token für die exakte Ressource `/mcp` und den erforderlichen Scope. Tokens werden nicht an die Lightroom-Bridge weitergereicht.
- Zufällige Codes/Tokens mit 256 Bit Entropie; Codes 60 Sekunden, Access Tokens 15 Minuten, rotierende Refresh Tokens maximal 24 Stunden ab Erstfreigabe. Refresh-Replay widerruft die gesamte Tokenfamilie. `/revoke` widerruft Access und Refresh gemeinsam.
- Nur SHA-256-Schlüssel der MCP-Bearerwerte im Speicher. GitHub-Token nur für die Identitätsprüfung, weder persistiert noch an ChatGPT ausgegeben. Access-Logging für den HTTP-Server ist aus, damit Callback-Codes nicht in URL-Logs landen.
- Client-Metadaten ohne Client-Secrets werden in `~/.config/mhphotos-lightroom/oauth-clients.sqlite3` (600) aufbewahrt. Tokens und offene Anmeldungen sind flüchtig: Neustart widerruft sie, vorhandene Client-IDs bleiben gültig und können neu autorisieren. Nur ein Serverprozess/Worker wird unterstützt.
- Öffentliche OAuth-Anfragen sind auf 120 pro Minute, 16 KiB Request-Body, 100 Clients, 100 offene Autorisierungen und begrenzte Tokenbestände beschränkt. Limits sind global; bei Störungen kann eine fremde Anfrageflut Anmeldungen vorübergehend blockieren.
- TLS terminiert bei Tailscale. Kein CIMD/private_key_jwt/mTLS-Nachweis der ChatGPT-Clientidentität; DCR + exakte Rückleitung + PKCE + Besitzeranmeldung schützen diese private Einzelbenutzer-Anbindung.

## Lokale Vorbereitung und Prüfung

Im Projektordner:

```zsh
cd /Volumes/Mac/CLAUDE/projekte/mhphotos-lightroom-mcp
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check server tests
chmod 700 ~/.config/mhphotos-lightroom
chmod 600 ~/.config/mhphotos-lightroom/github-client-secret
```

Das Secret niemals anzeigen. Die GitHub-App benötigt als Callback unverändert:
`https://macbook-air-von-martin.tailae2331.ts.net/auth/github/callback`.

Die exakte öffentliche ChatGPT-Rückleitungsadresse kopieren, danach im separaten Terminal starten:

```zsh
export MHPHOTOS_PUBLIC_URL='https://macbook-air-von-martin.tailae2331.ts.net'
export MHPHOTOS_CHATGPT_REDIRECT_URIS='HIER_DIE_EXAKTE_CHATGPT_RUECKLEITUNGSADRESSE'
export MHPHOTOS_MCP_TRANSPORT='streamable-http'
export MHPHOTOS_BRIDGE_TOKEN="$(cat ~/.mhphotos-lightroom-token)"
.venv/bin/mhphotos-lightroom-mcp
```

Ohne URL, Callback-Allowlist oder korrekt geschütztes Secret startet der HTTP-Modus nicht. Eventuell noch laufenden alten, ungeschützten HTTP-Prozess auf 8766 zuvor beenden. Claude-stdio und Bridge weiterlaufen lassen.

In einem zweiten Terminal:

```zsh
curl -i http://127.0.0.1:8766/mcp
curl -i -H 'Authorization: Bearer invalid' http://127.0.0.1:8766/mcp
curl -fsS http://127.0.0.1:8766/.well-known/oauth-authorization-server
curl -fsS http://127.0.0.1:8766/.well-known/oauth-protected-resource/mcp
```

Beide MCP-Anfragen müssen **401** mit `WWW-Authenticate` liefern. Discovery muss HTTPS-Endpunkte, `S256`, `none`, den Scope und die exakte Ressource ausweisen. Die automatisierten HTTP-Tests prüfen den vollständigen Fluss mit simuliertem GitHub, inklusive Tokenverwendung am Streamable-HTTP-Endpunkt und relevanter Ablehnungsfälle. Sie ersetzen keinen echten GitHub-/ChatGPT-Browsertest.

## Erst nach bestandener Auth-Prüfung veröffentlichen

Der Agent aktiviert Funnel nicht automatisch. Prüfe zunächst den bestehenden Tailscale-Status und die Syntax der installierten Version:

```zsh
tailscale funnel status
tailscale funnel --help
```

Wenn keine bestehende Konfiguration überschrieben wird, ist der übliche Start für diese Version:

```zsh
tailscale funnel --bg http://127.0.0.1:8766
```

Nur **8766**, niemals **8765**, weiterleiten. Danach extern dieselben 401-/Discovery-Prüfungen gegen die HTTPS-Adresse durchführen. Bei unerwartetem Ergebnis sofort deaktivieren:

```zsh
tailscale funnel --https=443 off
```

In ChatGPT den benutzerdefinierten MCP-Server mit URL
`https://macbook-air-von-martin.tailae2331.ts.net/mcp`, Authentifizierung **OAuth** und dynamischer Clientregistrierung verbinden. Die GitHub-Client-ID und das GitHub-Secret hier nicht eingeben. Mit `blackluzie` anmelden und die Lesefreigabe bestätigen. Beide Tools ausprobieren. Mit einem anderen GitHub-Konto muss die Anmeldung scheitern. Nach Serverneustart neu autorisieren; spätestens nach 24 Stunden ist erneute GitHub-Anmeldung erforderlich.

Sollte ChatGPT zunächst die Callback-URL erst nach einer fehlgeschlagenen Verbindung anzeigen, diese öffentliche URL aus der Verwaltungsseite übernehmen, den Server mit aktualisierter Allowlist starten und erneut verbinden. Keine Callback-Wildcards zulassen.

## Quellen, geprüft am 11. Oktober 2026

- [OpenAI: MCP authentication](https://developers.openai.com/plugins/build/auth)
- [MCP Authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [GitHub: Authorizing OAuth apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
- Lokaler Quellcode von MCP SDK 1.30.0: AuthSettings, AuthorizationHandler, TokenHandler, FastMCP und Bearer-Middleware. Ergänzungen gegenüber SDK-Defaults: richtige `none`-Discovery, Prüfung des Token-Request-Resource-Parameters, exakte Callback-Allowlist, persistente öffentliche Clientregistrierung und ausgeschaltete URL-Zugriffslogs.

## Betrieb ohne Terminalfenster

Auf diesem Mac sind Benutzer-Hintergrunddienste `de.mhphotos.lightroom.bridge` und
`de.mhphotos.lightroom.http` in `~/Library/LaunchAgents/` installiert und gestartet.
Sie starten bei der Benutzeranmeldung und werden nach einem Prozessabbruch neu gestartet.
Beide lesen den bestehenden Bridge-Token aus `~/.mhphotos-lightroom-token` (600).
Die öffentliche URL und die exakten ChatGPT-Rückleitungsadressen stehen ohne Secrets in
`~/.config/mhphotos-lightroom/desktop.json` (600). Logs liegen geschützt in
`~/Library/Logs/MHPhotos/`.

Die Projektdateien und das Python-Environment unter `/Volumes/Mac/CLAUDE/projekte/mhphotos-lightroom-mcp`
müssen erreichbar bleiben. Der Mac muss eingeschaltet und wach sein; der angemeldete
Benutzer und Tailscale müssen aktiv bleiben. Nach MCP-Neustarts ist die ChatGPT-App erneut
zu verbinden, weil Access- und Refresh-Tokens weiterhin bewusst flüchtig sind.

Lightroom liest den lokalen Token jetzt automatisch. Nach Aktualisierung des Plugins
im Zusatzmodul-Manager genügt weiterhin die ausdrückliche Aktion
„MHPhotos: Send Selected Metadata“ mit Bestätigung; keine Token-Eingabe und keine
Terminalfenster. Es werden weiterhin nur Dateiname, Bewertung und Markierungsstatus
von maximal fünf ausgewählten Fotos übermittelt.
