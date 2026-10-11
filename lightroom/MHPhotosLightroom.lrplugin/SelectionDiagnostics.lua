-- Read-only diagnostics. No files, catalog records, or network are modified.
local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrTasks = import "LrTasks"

LrTasks.startAsyncTask(function()
    local ok, result = pcall(function()
        local catalog = LrApplication.activeCatalog()
        if not catalog then
            return "No active Lightroom catalog."
        end

        -- The Lightroom SDK returns the current target photo selection.
        -- Only the count is displayed; no paths or metadata are exported.
        local photos = catalog:getTargetPhotos()
        return string.format("Selected photos: %d\nNo changes were made.", #photos)
    end)

    if ok then
        LrDialogs.message("MHPhotos Lightroom MCP", result, "info")
    else
        LrDialogs.message(
            "MHPhotos Lightroom MCP",
            "Could not read selection diagnostics. Check Lightroom plug-in logs.",
            "warning"
        )
    end
end)
