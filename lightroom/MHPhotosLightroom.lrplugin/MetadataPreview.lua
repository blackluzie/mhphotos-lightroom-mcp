-- Read-only preview of a small allowlist of Lightroom SDK metadata.
-- Nothing is sent over the network or written to disk.
local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrTasks = import "LrTasks"

local MAX_PHOTOS = 5

local Metadata = require "SelectionMetadata"
local details = {
    { "dateTimeOriginalISO8601", "Capture time" },
    { "cameraMake", "Camera make" }, { "cameraModel", "Camera" },
    { "lens", "Lens" }, { "shutterSpeed", "Shutter" }, { "aperture", "Aperture" },
    { "isoSpeedRating", "ISO" }, { "focalLength", "Focal length" },
    { "dimensions", "Original dimensions" }, { "croppedDimensions", "Cropped dimensions" },
    { "fileType", "File type" },
}

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
                local photo = Metadata.read(photos[i])
                lines[#lines + 1] = string.format(
                    "%d. %s | Rating: %s | Flag: %s",
                    i,
                    photo.fileName,
                    photo.rating,
                    photo.pickStatus
                )
                for _, field in ipairs(details) do
                    if photo[field[1]] then
                        lines[#lines + 1] = "    " .. field[2] .. ": " .. photo[field[1]]
                    end
                end
            end
        end)
        lines[#lines + 1] = "Read-only preview. No changes made."
        return table.concat(lines, "\n")
    end)()
    LrDialogs.message("MHPhotos Metadata Preview", message, "info")
end)
