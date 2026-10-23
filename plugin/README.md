# Lightroom Classic plugin (Lua)

`mhphotos.lrdevplugin` is the Lightroom side of the bridge. It long-polls the local MCP
server and executes commands with the Lightroom SDK. **Status: written against the SDK
docs and unit-tested with a simulated API. First run in real Lightroom Classic 15.6 on a disposable
catalog: see the ticked list below.**

## Install
1. Lightroom Classic → *File → Plug-in Manager… → Add* → select the folder
   `plugin/mhphotos.lrdevplugin`. Status should show "Installed and running".
2. Start the MCP server in bridge mode (it creates `~/.mhphotos-lightroom-mcp/bridge.json`):
   ```bash
   pip install -e .
   MHPHOTOS_ADAPTER=bridge mhphotos-lightroom-mcp
   ```
   For an MCP client, put the same environment into its server config.
3. The plugin starts its polling loop by itself when Lightroom loads it (`LrForceInitPlugin`); after a server restart it
   reconnects within about 10 seconds. *Library → Plug-in Extras → MHPhotos MCP: Status* shows the loop state and the
   same text is in `~/.mhphotos-lightroom-mcp/plugin-status.txt` (start/failure trace: `plugin-init.log`).

## First test (use a disposable catalog with ~10 photos, never your production catalog)

**Step 0: self test.** Library > Plug-in Extras > *MHPhotos MCP: Selbsttest (nur lesen)* (or ask Claude for `lightroom_selftest`).
It runs every read command on a few photos and shows each failure with Lightroom's own error text, plus
`~/.mhphotos-lightroom-mcp/selftest.json`. Fix those first; the list below goes deeper.

Run in this order and tick off each step. Ticked = confirmed in real Lightroom Classic 15.6 on 2026-10-16 by reading
the result back through the tools (not every item was also compared by eye in Lightroom's own panels). Unticked =
not yet checked, or checked with a note:

- [x] `connection_status` → `connected: true, mode: bridge`
- [x] `list_collections` matches Lightroom (names, counts; sets appear as `Set/Name`)
- [x] `list_photos` (whole catalog, one collection, paging with limit/offset)
- [ ] `list_photos` filters: `min_rating=5`, `year=…`, `keyword=…`, `flag=picked` (results match what Lightroom shows)
- [ ] `get_photo_metadata`: filename, rating, flag, keywords, camera correct;
      `capture_time` plausible (see note below)
- [ ] `get_photo_previews` shows real images (try 2 photos, size 512)
- [ ] `get_photo_metadata`: `location`, `gps`, `iso`, `aperture`, `shutter_speed`, `lens` plausible
- [ ] `list_photos` with `location=…` and with `near_lat/near_lon/radius_km`
- [x] `get_develop_settings` returns the values you see in the Develop module
- [x] `list_presets` lists your presets (folder + name)
- [ ] Edits disabled by default: `set_rating … confirm=true` → `code: disabled`
- [x] Restart server with `MHPHOTOS_ALLOW_WRITES=1`: `set_rating` without confirm → dry run only
- [x] `set_rating`, `set_flag`, `add_keywords` with `confirm=true` → visible in Lightroom
- [x] Rating 0 clears the rating
- [x] `undo_last_change` after a rating/flag/keyword change restores the old values
- [x] `create_collection` + `add_to_collection`: album appears, photos inside
- [x] `set_color_label` (green) shows in Lightroom; `none` clears it
- [x] `set_title_caption`: visible in the Metadata panel; existing text is not overwritten without `overwrite=true`
- [x] `create_virtual_copies`: copy appears next to the original, original unchanged
- [x] `get_contact_sheet` with 6 photos: one numbered image
- [ ] `list_collections(include_counts=true)` counts match; without it the call is fast
- [ ] `apply_preset` (dry run, then confirm) on a test photo: preset visible in Develop, one history step "MHPhotos…", undoable
- [x] `apply_develop_settings` e.g. `{"Exposure2012": 0.5}`: slider moves
- [ ] `create_preset`: preset appears under the plugin's folder in the Develop module's preset list (**not confirmed**: Lightroom
      accepts it and returns a handle, but neither `list_presets` nor the Develop panel shows it. The plugin keeps the handle until
      the plugin is reloaded, so `apply_preset` by name should work in the same run; that is covered by the Lua tests but **not yet
      tried in Lightroom**; see Known behaviours)
- [x] A bad id in a batch changes nothing
- [x] Restart with `MHPHOTOS_EXPORT_DIR=/some/abs/folder`: `export_photos` dry run, then confirm → JPEGs appear
- [x] Quit Lightroom → tools return `unavailable`; restart Lightroom → reconnects by itself (2026-10-17: the plugin starts at launch; the server saw it 21 s after its own start)
- [x] Restart the MCP server (new token) → plugin reconnects without restarting Lightroom

## Auto-export (optional, off by default)

The plugin can export the best new photos of one collection by itself, so they reach the MHPhotos
inbox without you or Claude doing anything (for example while the Mac runs at home during a cruise).
Together with the hot-folder service (`install-service`) the chain is: Lightroom collection ->
JPEG in `~/Pictures/MHPhotos-Inbox` -> inbox with status "wartet". Nothing is published.

Easiest: *Library > Plug-in Extras > MHPhotos MCP: Auto-Export einrichten* opens a small window (collection name, typed in,
minimum rating, photos per round, interval, dry run, active) and writes the file for you. By hand, create
`~/.mhphotos-lightroom-mcp/autoexport.json` (it is read again every round, no reload needed):

```json
{ "enabled": true, "collection": "Kreuzfahrt", "min_rating": 4, "max_per_run": 5,
  "interval_minutes": 15, "dry_run": true }
```

- `dry_run` stays `true` until you set `false` explicitly: first it only reports what it would export.
- Each photo is exported once (`autoexport-done.json`), at most `max_per_run` per round.
- `dest_dir` defaults to `~/Pictures/MHPhotos-Inbox` (absolute path or `~/...`).
- It only reads the catalog and exports files; it never changes ratings, keywords or collections.
- It is configured only by this file, never over the MCP bridge.
- State: `~/.mhphotos-lightroom-mcp/autoexport-status.txt`.

## What was checked against the SDK reference, and what was not
Checked on 2026-10-16 against a mirror of Adobe's Lightroom Classic SDK reference
([lrc.mcor.dev](https://lrc.mcor.dev/), not Adobe itself): every function and metadata key the plugin uses **exists**
with the signature used here: `findPhotoByUuid`, `getCollectionByLocalIdentifier`, `createCollection(name, parent,
canReturnPrior)`, `createKeyword(...)`, `withWriteAccessDo(name, fn, {timeout, asynchronous})`, `createVirtualCopies`,
`setSelectedPhotos`, `setActiveSources`, `photo:requestJpegThumbnail(w, h, callback)`, `getDevelopSettings`,
`applyDevelopPreset(preset, plugin)`, `applyDevelopSettings`, `addKeyword`/`removeKeyword`, `setRawMetadata` for
`rating`, `pickStatus`, `colorNameForLabel`, `title`, `caption`, `LrApplication.developPresetFolders`,
`addDevelopPresetForPlugin`, `LrFileUtils.readFile`, `LrHttp.post` (status in `headers.status`). The self test also
confirms at run time that your Lightroom version has them ("API present: ...").

Still **not verifiable without Lightroom** (behaviour, not names):
- The export setting keys (`LR_format`, `LR_export_destinationType`, ...) are not listed in the reference; they follow
  the keys Lightroom itself writes into export presets. `export_photos` is the first thing to test.
- Setting `rating` to `nil` to clear it (rating 0).
- `title`/`caption` are read with `getFormattedMetadata` (they are not raw-readable) and written with `setRawMetadata`.
- `capture_time`: Lightroom's raw time is relative to 2001-01-01 and has no time zone; the plugin presents it as UTC.

Known Lightroom behaviours to watch:
- **Lightroom's `pcall` cannot yield.** Catalog calls yield, so the plugin uses `LrTasks.pcall` wherever Lightroom code runs
  inside a protected call. The Lua tests' fake API never yields and cannot catch this.
- **The plugin's JSON encoder writes an empty table as `[]`.** The server therefore accepts `[]` as an empty object.
- **Presets made by `create_preset`** are not listed by `LrApplication.developPresetFolders()` and did not show in the Develop
  panel in the first run (Lightroom 15.6). `addDevelopPresetForPlugin` does return a handle; the plugin keeps it until reload
  and `create_preset` reports `returned_by_lightroom` and `listed_in_presets`.
- Lightroom reports "no colour" as `grey`; the tools map it to `none` (confirmed in Lightroom).
- **Virtual copies** are only made for the *selected* photos, a selection only holds photos of the *active source*,
  and Adobe's forum reports it works reliably only in the Library grid. The plugin therefore switches to the Library
  grid, sets the source "All Photographs", selects the one photo and creates its copy (named "MHPhotos"). Your
  selection and view change as a side effect.
- **Presets created by `create_preset`** are stored under the plugin's own folder. A 2026 forum thread reports that
  applying a plugin-created preset shifted **white balance** on Lightroom 15.0.1 although the preset had none (not
  reproducible on 15.1). After the first `apply_preset` with such a preset, check the white balance.
- Develop settings are marked *experimental* in the SDK; the keys may change in future Lightroom versions.
- Collection **sets** cannot hold photos: ids of sets are rejected with a clear message.

Errors from the plugin show up in the tool result (`code: plugin_error`). Use the
*Status* menu item for the last command and connection state.

## Lua tests (no Lightroom needed)
```bash
cd plugin/tests && lua5.1 json_test.lua && lua5.1 commands_test.lua
```
Homebrew no longer ships `lua@5.1`; `luajit` (`brew install luajit`) runs the same tests.
