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

## Things most likely to need a fix on the first real run
These API usages are the ones not verifiable without Lightroom:
- `catalog:findPhotoByUuid`, `catalog:getCollectionByLocalIdentifier`
- `catalog:withWriteAccessDo(name, fn, {timeout = 15})` return value `'executed'`
- `catalog:createKeyword(name, {}, true, nil, true)`
- `photo:requestJpegThumbnail`, `LrStringUtils.encodeBase64`, `photo:getDevelopSettings`
- `LrApplication.developPresetFolders`, `photo:applyDevelopPreset(preset, _PLUGIN)`, `photo:applyDevelopSettings`, `LrApplication.addDevelopPresetForPlugin`
- `catalog:createCollection(name, nil, true)`, `collection:addPhotos`, `setRawMetadata('colorNameForLabel' / 'title' / 'caption')`, `photo:removeKeyword`
- `catalog:setSelectedPhotos` + `catalog:createVirtualCopies` (one copy per call)
- `LrExportSession` settings keys, and `LrFileUtils.readFile`
- `capture_time`: Lightroom's raw time is relative to 2001-01-01 and has no time zone;
  the plugin presents it as if it were UTC.

Errors from the plugin show up in the tool result (`code: plugin_error`). Use the
*Status* menu item for the last command and connection state.

## Lua tests (no Lightroom needed)
```bash
cd plugin/tests && lua5.1 json_test.lua && lua5.1 commands_test.lua
```
