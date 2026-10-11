return {
    LrSdkVersion = 6.0,
    LrToolkitIdentifier = "de.mhphotos.lightroom.mcp",
    LrPluginName = "MHPhotos Lightroom MCP",
    LrInitPlugin = "Init.lua",
    LrShutdownPlugin = "Shutdown.lua",
    LrExportMenuItems = {
        {
            title = "MHPhotos: Show Connection Status",
            file = "Status.lua",
        },
    },
}
