local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrTasks = import "LrTasks"
local Collections = require "CollectionMetadata"

LrTasks.startAsyncTask(function()
    local ok, overview = LrTasks.pcall(function()
        local catalog = LrApplication.activeCatalog()
        if not catalog then error("No active catalog") end
        return Collections.read(catalog)
    end)
    if not ok then
        LrDialogs.message("MHPhotos", "Could not read the collection overview. No data sent.", "warning")
        return
    end
    if LrDialogs.confirm("MHPhotos Collection Overview", Collections.preview(overview),
                         "Send overview", "Cancel") ~= "ok" then return end
    local sent = LrTasks.pcall(function()
        local Transfer = require "CollectionTransfer"
        Transfer.send(Collections.encode(overview))
    end)
    if sent then
        LrDialogs.message("MHPhotos", "Collection overview sent. No catalog changes made.", "info")
    else
        LrDialogs.message("MHPhotos", "Transfer failed. Use the updated MHPhotos helper and bridge.", "warning")
    end
end)
