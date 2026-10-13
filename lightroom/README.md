# Lightroom Classic plugin (experimental)

This plugin can preview a limited selection of photo metadata and includes an
**explicit, user-triggered** localhost transfer action. The Lua code has **not**
yet been tested inside Lightroom Classic.

## Development installation on macOS

1. Use a dedicated Lightroom Classic test catalog with sample photos.
2. Start the Python bridge with a unique 32+ character token in the
   `MHPHOTOS_BRIDGE_TOKEN` environment variable.
3. In Lightroom Classic, open **File > Plug-in Manager**, click **Add**, and
   select `lightroom/MHPhotosLightroom.lrplugin`.
4. Find the MHPhotos commands in Lightroom's **Library > Plug-in Extras**
   menu (the precise menu placement may vary by Lightroom version).
5. Run **MHPhotos: Preview Selected Metadata** first.
6. Only when ready, run **MHPhotos: Send Selected Metadata**, confirm the transfer. The plugin reads the existing
   `~/.mhphotos-lightroom-token` automatically; no token entry is needed.
7. The Python MCP tool `selected_photo_metadata` reads the most recent
   successfully submitted selection.

## Data and security boundaries

- At most five selected photos per submission.
- Only `fileName`, `rating` and `pickStatus` are submitted.
- The destination is `http://127.0.0.1:8765/v1/selection`, not a remote host.
- The token is read from the owner-local token file and is not written to Lightroom preferences.
- The bridge holds a snapshot in RAM for at most five minutes; it does not modify catalog records.
- No automatic background synchronization, image upload, GPS or full paths.
- Do not expose the local listener to the LAN or Internet.

## Validation status

Python unit and integration tests exercise the HTTP-to-MCP path, but the Lua
plugin and Lightroom SDK integration require an actual Lightroom Classic
installation on macOS. See `docs/integration-test-plan.md`.
