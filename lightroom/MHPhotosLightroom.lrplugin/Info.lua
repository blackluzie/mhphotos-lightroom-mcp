return {
    LrSdkVersion = 6.0,
    LrToolkitIdentifier = "de.mhphotos.lightroom.mcp",
    LrPluginName = "MHPhotos Lightroom MCP",
    LrInitPlugin = "Init.lua",
    LrShutdownPlugin = "Shutdown.lua",
    LrLibraryMenuItems = {
        {
            title = "MHPhotos: Send Selected Metadata",
            file = "SendSelection.lua",
        },
        {
            title = "MHPhotos: Preview Selected Metadata",
            file = "MetadataPreview.lua",
        },
        {
            title = "MHPhotos: Show Selection Diagnostics",
            file = "SelectionDiagnostics.lua",
        },
        {
            title = "MHPhotos: Show Connection Status",
            file = "Status.lua",
        },
    },
}
