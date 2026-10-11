# Lightroom Classic plugin scaffold

This is an **inert, read-only starting point** for the Lightroom Classic Lua
plugin. It does not yet communicate with the Python MCP server.

## Install for development (when at the Mac)

1. Open Lightroom Classic > File > Plug-in Manager.
2. Click **Add**, then choose `lightroom/MHPhotosLightroom.lrplugin`.
3. In Lightroom Classic, use File > Plug-in Extras > **MHPhotos: Show Connection Status**
   (the menu location may vary by Lightroom Classic version).
4. Confirm the informational message. No Lightroom data is read or modified.

## Security and next steps

- Use a **dedicated test catalog** for all first integration tests.
- Do not expose a listener on public or LAN interfaces.
- Never write Lightroom catalog SQLite files directly.
- Next: implement a limited catalog metadata reader through documented Lightroom SDK
  calls, then design an authenticated localhost exchange with the Python bridge.
- The current Python `bridge.py` is only a **client**. A corresponding listener
  does not exist yet.

## Notes

The Lua files must be validated inside Lightroom Classic on macOS; GitHub's
Python CI cannot execute Lightroom's proprietary Lua SDK.
