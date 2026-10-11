-- Read-only preview of a small allowlist of Lightroom SDK metadata.
-- Nothing is sent over the network or written to disk.
local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrTasks = import "LrTasks"

local MAX_PHOTOS = 5

local function safeValue(photo, field)
    local ok, value = pcall(function()
        return photo:getRawMetadata(field)
    end)
    if not ok or value == nil then
        return "n/a"
    end
    if type(value) ~= "string" and type(value) ~= "number" then
        return "n/a"
    end
    local text = tostring(value)
    -- Keep dialogs small and avoid control characters in displayed values.
    text = text:gsub("[%c]", " ")
    return text:sub(1, 80)
end

LrTasks.startAsyncTask(function()
    local ok, message = pcall(function()
        local catalog = LrApplication.activeCatalog()
        if not catalog then
            return "No active catalog."
        end
        local photos = catalog:getTargetPhotos()
        local count = math.min(#photos, MAX_PHOTOS)
        local lines = {
            string.format("Selected: %d | Previewing: %d", #photos, count),
        }
        for i = 1, count do
            local photo = photos[i]
            lines[#lines + 1] = string.format(
                "%d. %s | Rating: %s | Flag: %s",
                i,
                safeValue(photo, "fileName"),
                safeValue(photo, "rating"),
                safeValue(photo, "pickStatus")
            )
        end
        lines[#lines + 1] = "Read-only preview. No changes made."
        return table.concat(lines, "\n")
    end)
    if ok then
        LrDialogs.message("MHPhotos Metadata Preview", message, "info")
    else
        LrDialogs.message(
            "MHPhotos Metadata Preview",
            "Preview failed. No catalog changes were made.",
            "warning"
        )
    end
end)
