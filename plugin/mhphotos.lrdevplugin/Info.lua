return {
  LrSdkVersion = 6.0,
  LrSdkMinimumVersion = 6.0,
  LrToolkitIdentifier = 'de.mhphotos.lightroom-mcp',
  LrPluginName = 'MHPhotos MCP Bridge',
  LrInitPlugin = 'Init.lua',
  LrShutdownPlugin = 'Shutdown.lua',
  LrLibraryMenuItems = {
    { title = 'MHPhotos MCP: Status', file = 'Status.lua' },
  },
  VERSION = { major = 0, minor = 1, revision = 0, build = 1 },
}
