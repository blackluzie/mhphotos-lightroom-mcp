-- Read-only preview of a small allowlist of Lightroom SDK metadata.
-- Nothing is sent over the network or written to disk.
local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrTasks = import "LrTasks"

local MAX_PHOTOS = 5

local function safeValue(photo, field)
    local value = photo:getRawMetadata(field)
    if value == nil then
        return "nil"
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
    local message = (function()
        local catalog = LrApplication.activeCatalog()
        if not catalog then
            return "No active catalog."
        end
        local photos = catalog:getTargetPhotos()
        local count = math.min(#photos, MAX_PHOTOS)
        local lines = {
            string.format("Selected: %d | Previewing: %d", #photos, count),
        }
        catalog:withReadAccessDo(function()
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
        end)
        lines[#lines + 1] = "Read-only preview. No changes made."
        return table.concat(lines, "\n")
    end)()
    LrDialogs.message("MHPhotos Metadata Preview", message, "info")
end)
