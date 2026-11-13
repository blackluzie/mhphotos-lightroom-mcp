# MHPhotos Lightroom MCP

Open-source Model Context Protocol server for Adobe Lightroom Classic. Ask Claude (or another MCP
client) to find, look at, rate, edit and publish your photos, with a safety gate in front of everything
that changes something.

> **Status:** reads, edits and the MHPhotos workflow are implemented and tested against a simulated
> Lightroom. The Lightroom plugin has had a **first run in real Lightroom Classic 15.6** (disposable catalog):
> self test 16/16, export, rating/flag/keywords with undo, colour labels, title/caption, virtual copies, collections,
> contact sheet and develop settings work. A server restart (new token) and a Lightroom restart reconnect by themselves. Not yet confirmed in real
> Lightroom: applying a preset made by `create_preset` (Lightroom does not list it; see [plugin/README.md](plugin/README.md)).
> Use a disposable catalog first and start with the plugin self test. Not affiliated with Adobe.

## Setup on a Mac in 5 steps (details: [docs/setup-mac.md](docs/setup-mac.md))
```bash
uvx --from git+https://github.com/blackluzie/mhphotos-lightroom-mcp mhphotos-lightroom-mcp install-plugin
# Lightroom Classic > File > Plug-in Manager > Add > ~/Documents/MHPhotos-Lightroom-Plugin/mhphotos.lrdevplugin
claude mcp add mhphotos-lightroom --env MHPHOTOS_ADAPTER=bridge -- \
  uvx --from git+https://github.com/blackluzie/mhphotos-lightroom-mcp mhphotos-lightroom-mcp
# then, in Claude:  "run lightroom_selftest"        (or in a terminal:  mhphotos-lightroom-mcp doctor)
```
From a clone instead: `pip install -e '.[dev]'` (then `mhphotos-lightroom-mcp ...`); the committed `.mcp.json` starts the server
in bridge mode with reads only.

## Skill `/mhphotos` (Foto-Durchsicht)

`skills/mhphotos/SKILL.md` packt den Ablauf der Durchsicht zusammen: einen Monat, eine Reise oder eine Sammlung sichten, Sterne,
Fotobuch- und Aussortieren-Vorschläge, Titel und Stichwörter, S/W, Presets, 4:5/9:16-Zuschnitte und Begradigung.
Aufruf zum Beispiel `/mhphotos 2026/10` oder „alle Fotos aus 2026/09 Südtirol in ein Album legen und optimieren“.
Installation: in Claude Code den Ordner `skills/mhphotos` nach `~/.claude/skills/` kopieren; in claude.ai den Ordner als ZIP unter
Einstellungen → Fähigkeiten (Skills) hochladen. Der MHPhotos-Lightroom-Connector muss verbunden sein.

## Remote clients and rebuilding

The private HTTP deployment supports explicitly allowlisted OAuth MCP clients.
See [other clients](docs/mcp-clients.md), [automated Mac rebuild and encrypted recovery](docs/rebuild.md),
[HTTP read/export boundary](docs/http-read-export.md) and [private n8n JPEG delivery](docs/private-assets.md).
The broad tool list below describes the local server; the public HTTP service exposes only its restricted read/export subset.

## Tools
Everything is read-only unless noted. Changes are a **dry run** unless `confirm=true`, need an operator
opt-in (environment variable) and, when your client supports it, **you are asked directly** (yes/no).

| Group | Tools |
|-------|-------|
| Look & search | `connection_status`, `lightroom_selftest`, `list_collections` (`name_contains`), `list_photos` (`min_rating`, `year`/dates, `keyword`, `flag`, `location`, GPS circle), `get_photo_metadata`, `get_photo_previews` (images), `get_contact_sheet` (one numbered overview image), `get_develop_settings`, `list_presets`, `backlog_report`, `list_changes`, `catalog_audit_start` / `catalog_audit_status` / `catalog_audit_cancel` / `catalog_audit_results` (wrong date folder, duplicates, storage), `photos_by_date` (fast date queries: unreviewed, best, missing captions/GPS), `suggest_locations` |
| Organise (`MHPHOTOS_ALLOW_WRITES=1`) | `set_rating`, `set_flag`, `set_color_label`, `set_title_caption`, `add_keywords`, `create_collection` (`parent` = Sammlungsgruppe), `move_collection`, `create_smart_collection`, `create_keyword` (Hierarchie `A > B > C`, Synonyme), `import_photo`, `set_custom_metadata` (eigene Felder „MHPhotos-Status“/„Notiz“, derzeit abgeschaltet: Lightroom 15 lehnte die Felddefinition ab), `add_to_collection`, `remove_from_collection` (nimmt Fotos nur aus dem Album, `all_photos=true` leert es), `undo_last_change`, `catalog_audit_collections`, `set_location`, `rotate_photos` (90/180/270° clockwise), `crop_to_aspect` (virtual copies cropped to 4:5, 9:16 …; `angle` also levels a horizon) |
| Change files (`MHPHOTOS_ALLOW_FILE_EDITS=1`, needs the edit opt-in) | `write_name_dates_to_files` (writes a capture time from the file name into JPEGs with exiftool, backup first) |
| Edit looks (`MHPHOTOS_ALLOW_WRITES=1`) | `apply_preset`, `apply_develop_settings`, `sync_develop_settings`, `create_preset`, `create_virtual_copies`, `build_smart_previews`, `auto_tone` (Lightrooms Auto-Ton, Foto für Foto), `list_webhooks`/`send_webhook` (n8n u. ä., Adressen nur aus `webhooks.json` auf dem Mac) |
| Catalog insights (0.1.26, from the last catalog check) | `catalog_audit_overview` (per year), `catalog_folder_moves` (move list per folder), `archive_candidates`, `gear_usage`, `presort_status` / `presort_month` (night pre-sorting), `keyword_check_start` / `keyword_check_status` / `keyword_check_results`, `trip_outline`; 0.1.27: `duplicate_folders`, `missing_files_by_drive`, `gps_gaps`, `print_candidates`, `calendar_candidates`, `weekly_report`; 0.1.29: `photo_stats` (offline statistics page: cameras, lenses, focal lengths, aperture/ISO, hit rate, golden hour, unused lenses, prime suggestion, never-touched storage) |
| Organise more (`MHPHOTOS_ALLOW_WRITES=1`) | `archive_candidates_collections`, `presort_apply` (review collections "Vorsortieren", never stars), `merge_keywords`, `mark_online_in_lightroom` (keywords "MHPhotos-Online"/"Instagram" + smart collections), `spot_photos_collections`, `fill_gps_gaps`, `print_candidates_collections`, `calendar_collections` |
| Export | `export_photos` (`MHPHOTOS_EXPORT_DIR`) |
| MHPhotos site | `mhphotos_galleries`, `mhphotos_inbox`, `mhphotos_inbox_images`, `mhphotos_inbox_contact_sheet`, `mhphotos_edit_preview` (basic edit before/after), `mhphotos_analyze_inbox`, `mhphotos_instagram_posts`, `mhphotos_instagram_images`, `mhphotos_sync_instagram` (refresh the Instagram library), `send_to_mhphotos_inbox`, `mhphotos_publish`, `mhphotos_update_site_photo`, `mhphotos_link_instagram` (public site, `MHPHOTOS_ALLOW_PUBLISH=1`), `mhphotos_schedule_instagram` / `mhphotos_instagram_queue` / `mhphotos_cancel_instagram` (social queue, `MHPHOTOS_ALLOW_INSTAGRAM=1`), `instagram_candidates`, `mhphotos_best_times`, `spot_photos`, `mhphotos_sync_spot_photos` / `mhphotos_sync_gear_usage` (admin statistics), `mhphotos_create_blog_draft` (unpublished draft), `mhphotos_comments` / `mhphotos_comment_draft` / `mhphotos_comment_respond` (comments under own posts: read, AI draft, reply/like after confirm, `MHPHOTOS_ALLOW_INSTAGRAM=1`), `mhphotos_friend_posts` / `mhphotos_friend_post` / `mhphotos_friends` / `mhphotos_set_friend` (comment ideas for watched accounts to paste by hand, close friend or not) |

The MHPhotos tools also run **without Lightroom and without an MCP client**: `mhphotos-lightroom-mcp call TOOL '{json}'`
(images are saved as files). That is how they are used from a cloud session on a phone; see [docs/kreuzfahrt.md](docs/kreuzfahrt.md).

Example requests and the exact tool sequence behind them: [docs/workflows.md](docs/workflows.md).

## Configuration (environment of the MCP server)
| Variable | Meaning |
|----------|---------|
| `MHPHOTOS_ADAPTER` | `mock` (default, sample data) or `bridge` (real Lightroom) |
| `MHPHOTOS_ALLOW_WRITES` | `1` enables changes to the catalog (see groups above) |
| `MHPHOTOS_EXPORT_DIR` | absolute folder; enables export, inbox upload and scheduling a Lightroom photo for Instagram |
| `MHPHOTOS_INBOX_URL` | https URL of the MHPhotos app (enables the MHPhotos tools) |
| `MHPHOTOS_ADMIN_TOKEN` / `MHPHOTOS_ADMIN_TOKEN_FILE` / macOS Keychain `mhphotos-admin-token` | admin token of the MHPhotos app, looked up in this order. Prefer the Keychain: `security add-generic-password -s mhphotos-admin-token -a "$USER" -w` |
| `MHPHOTOS_ALLOW_PUBLISH` | `1` allows `mhphotos_publish`, `mhphotos_update_site_photo` and `mhphotos_link_instagram` (public site) |
| `MHPHOTOS_ALLOW_INSTAGRAM` | `1` allows scheduling/cancelling Instagram posts |
| `MHPHOTOS_CONFIRMATION` | `ask` (default), `require` (refuse if the client cannot ask you), `flag` (never ask) |
| `MHPHOTOS_TOOLSETS` | e.g. `read,develop`; limits the offered tools (`all`, `read`, `edit`, `develop`, `mhphotos`) |
| `MHPHOTOS_BRIDGE_PORT`, `MHPHOTOS_BRIDGE_TOKEN`, `MHPHOTOS_CONFIG_DIR` | bridge port (47831), fixed token, config/audit folder (`~/.mhphotos-lightroom-mcp`) |
| `MHPHOTOS_BRIDGE_TIMEOUT` | seconds (max 900): minimum time to wait for Lightroom per command, default off (each command keeps its own 15–600 s). Set e.g. `120` on a busy Mac so searches do not give up after 30 s. Does not shorten longer limits; the MCP client may have its own tool timeout (Claude Code: `MCP_TOOL_TIMEOUT`) |
| `MHPHOTOS_OAUTH_SESSION_DAYS` | public HTTP service only: how long a connector login lasts after its last token refresh (default 30, 1-90). Logins survive a server restart (only SHA-256 digests of the tokens are stored, in `oauth-clients.sqlite3`, mode 600) |

## Safety in short
- Loopback-only bridge with token; the plugin connects to the server, never the reverse.
- Reads need no opt-in. Changes: operator opt-in **and** `confirm=true` **and** (if supported) your yes/no.
- Every confirmed change is written to a local audit log (`list_changes`); rating, flag, color label,
  title/caption and keyword changes can be undone with `undo_last_change`.
- No deletes, no direct catalog database access, no secrets in the log or in tool results.
- Details: [docs/architecture.md](docs/architecture.md), [SECURITY.md](SECURITY.md).

## Repository
- `server/` Python MCP server, adapters, bridge, gate, audit, CLI (`doctor`, `install-plugin`)
- `plugin/` Lightroom Classic plugin (Lua) and its Lua tests
- `tests/` Python tests (tools, gate, bridge with a fake plugin, MHPhotos fake app, stdio end-to-end)
- `docs/` architecture, workflows, Mac setup, cloud API assessment

Lightroom Classic catalogs should be stored locally, even if originals live on mounted NAS volumes.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md). MIT licensed.

## Webhooks aus dem Plugin (Plugin ab 0.1.18)

`~/.mhphotos-lightroom-mcp/webhooks.json` auf dem Mac legt fest, welche Webhooks das Plugin aufrufen darf. Ein MCP-Client kann nur einen Namen nennen, nie eine Adresse; erlaubt sind `https://` und `http://localhost`/`127.0.0.1`, Nutzdaten bis 50 KB.

```json
{ "hooks": { "n8n-import": { "url": "https://n8n.example.com/webhook/abc", "token": "optional" } } }
```

`list_webhooks` zeigt die Namen, `send_webhook(name, payload)` ruft einen auf (Trockenlauf, dann `confirm=true`). Das Statusfenster im Library-Menü („MHPhotos MCP: Status“) zeigt Version, Verbindung, Zähler, letzte Fehler, Katalog und die Zahl der Webhooks.

## Veröffentlichungsdienst „MHPhotos (Foto-Inbox)“ (Plugin ab 0.1.19)

In Lightrooms Bereich „Veröffentlichungsdienste“ (Library, links) erscheint der Dienst **MHPhotos (Foto-Inbox)**. Fotos in eine seiner veröffentlichten Sammlungen ziehen und „Veröffentlichen“ klicken: Lightroom rendert sie als JPEG und legt sie in den Hot Folder (`~/Pictures/MHPhotos-Inbox`, oder `dest_dir` aus `autoexport.json`). Von dort läuft die bekannte Kette: Prüfung, Batch, n8n, MHPhotos-Foto-Inbox mit Status „wartet“. **Online geht nichts ohne Freigabe in MHPhotos**, und nach Instagram wird von hier nichts gesendet.

Lightroom zeigt pro Foto „Neu zu veröffentlichen“, „Veröffentlicht“ und bei Änderungen „Erneut veröffentlichen“. Entfernt man ein Foto aus der Sammlung, verschwindet nur die Lightroom-Markierung; in MHPhotos wird nichts gelöscht. Der Hot Folder und der Auto-Export laufen unverändert daneben. Macht der Dienst in Lightroom Probleme, die Zeile `LrExportServiceProvider` in `plugin/mhphotos.lrdevplugin/Info.lua` entfernen.
