return {
  LrSdkVersion = 6.0,
  LrSdkMinimumVersion = 6.0,
  LrToolkitIdentifier = 'de.mhphotos.lightroom-mcp',
  LrPluginName = 'MHPhotos MCP Bridge',
  LrInitPlugin = 'Init.lua',
  -- Without this Lightroom initialises a menu-only plugin lazily (on first menu use), so the
  -- polling loop in Init.lua would not start by itself after launching or reloading.
  LrForceInitPlugin = true,
  LrShutdownPlugin = 'Shutdown.lua',
  LrLibraryMenuItems = {
    { title = 'MHPhotos MCP: Status', file = 'Status.lua' },
    { title = 'MHPhotos MCP: Selbsttest (nur lesen)', file = 'SelfTestMenu.lua' },
    { title = 'MHPhotos MCP: Auto-Export einrichten', file = 'AutoExportSetup.lua' },
  },
  VERSION = { major = 0, minor = 1, revision = 0, build = 1 },
}
