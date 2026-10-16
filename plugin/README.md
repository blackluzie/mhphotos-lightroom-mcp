# Lightroom Classic plugin (Lua)

`mhphotos.lrdevplugin` is the Lightroom side of the bridge. It long-polls the local MCP
server and executes commands with the Lightroom SDK. **Status: written against the SDK
docs and unit-tested with a simulated API only. It has not yet run inside Lightroom.**

## Install
1. Lightroom Classic → *File → Plug-in Manager… → Add* → select the folder
   `plugin/mhphotos.lrdevplugin`. Status should show "Installed and running".
2. Start the MCP server in bridge mode (it creates `~/.mhphotos-lightroom-mcp/bridge.json`):
   ```bash
   pip install -e .
   MHPHOTOS_ADAPTER=bridge mhphotos-lightroom-mcp
   ```
   For an MCP client, put the same environment into its server config.
3. *Library → Plug-in Extras → MHPhotos MCP: Status* should say `connected` within ~10 s.

## First test (use a disposable catalog with ~10 photos, never your production catalog)

**Step 0: self test.** Library > Plug-in Extras > *MHPhotos MCP: Selbsttest (nur lesen)* (or ask Claude for `lightroom_selftest`).
It runs every read command on a few photos and shows each failure with Lightroom's own error text, plus
`~/.mhphotos-lightroom-mcp/selftest.json`. Fix those first; the list below goes deeper.

Run in this order and tick off each step:
- [ ] `connection_status` → `connected: true, mode: bridge`
- [ ] `list_collections` matches Lightroom (names, counts; sets appear as `Set/Name`)
- [ ] `list_photos` (whole catalog, one collection, paging with limit/offset)
- [ ] `list_photos` filters: `min_rating=5`, `year=…`, `keyword=…`, `flag=picked` (results match what Lightroom shows)
- [ ] `get_photo_metadata`: filename, rating, flag, keywords, camera correct;
      `capture_time` plausible (see note below)
- [ ] `get_photo_previews` shows real images (try 2 photos, size 512)
- [ ] `get_photo_metadata`: `location`, `gps`, `iso`, `aperture`, `shutter_speed`, `lens` plausible
- [ ] `list_photos` with `location=…` and with `near_lat/near_lon/radius_km`
- [ ] `get_develop_settings` returns the values you see in the Develop module
- [ ] `list_presets` lists your presets (folder + name)
- [ ] Edits disabled by default: `set_rating … confirm=true` → `code: disabled`
- [ ] Restart server with `MHPHOTOS_ALLOW_WRITES=1`: `set_rating` without confirm → dry run only
- [ ] `set_rating`, `set_flag`, `add_keywords` with `confirm=true` → visible in Lightroom
- [ ] Rating 0 clears the rating
- [ ] `undo_last_change` after a rating/flag/keyword change restores the old values
- [ ] `create_collection` + `add_to_collection`: album appears, photos inside
- [ ] `set_color_label` (green) shows in Lightroom; `none` clears it
- [ ] `set_title_caption`: visible in the Metadata panel; existing text is not overwritten without `overwrite=true`
- [ ] `create_virtual_copies`: copy appears next to the original, original unchanged
- [ ] `get_contact_sheet` with 6 photos: one numbered image
- [ ] `list_collections(include_counts=true)` counts match; without it the call is fast
- [ ] `apply_preset` (dry run, then confirm) on a test photo: preset visible in Develop, one history step "MHPhotos…", undoable
- [ ] `apply_develop_settings` e.g. `{"Exposure2012": 0.5}`: slider moves
- [ ] `create_preset`: preset appears under the plugin's folder in the Develop module's preset list
- [ ] A bad id in a batch changes nothing
- [ ] Restart with `MHPHOTOS_EXPORT_DIR=/some/abs/folder`: `export_photos` dry run, then confirm → JPEGs appear
- [ ] Quit Lightroom → tools return `unavailable`; restart Lightroom → reconnects by itself
- [ ] Restart the MCP server (new token) → plugin reconnects without restarting Lightroom

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
