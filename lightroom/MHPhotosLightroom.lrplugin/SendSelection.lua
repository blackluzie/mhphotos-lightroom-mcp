-- Explicit, user-triggered read-only selection transfer to local Python bridge.
-- The token is entered for this session; it is never persisted by this plugin.
local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrHttp = import "LrHttp"
local LrTasks = import "LrTasks"
local LrView = import "LrView"
local LrBinding = import "LrBinding"

local MAX_PHOTOS = 5
local URL = "http://127.0.0.1:8765/v1/selection"

local function value(photo, key)
    local result = photo:getRawMetadata(key)
    if result == nil then
        return ""
    end
    if type(result) ~= "string" and type(result) ~= "number" then
        return ""
    end
    return tostring(result):gsub("[%c]", " "):sub(1, 80)
end

local function jsonString(s)
    -- Only scalar strings are allowed; escape all JSON control characters.
    return '"' .. s:gsub('\\', '\\\\'):gsub('"', '\\"')
        :gsub("\b", "\\b"):gsub("\f", "\\f")
        :gsub("\n", "\\n"):gsub("\r", "\\r")
        :gsub("\t", "\\t"):gsub("[%z\1-\31]", function(c)
            return string.format("\\u%04x", string.byte(c))
        end) .. '"'
end

local function encode(photos)
    local parts = {}
    for _, photo in ipairs(photos) do
        parts[#parts + 1] = '{"fileName":' .. jsonString(photo.fileName)
            .. ',"rating":' .. jsonString(photo.rating)
            .. ',"pickStatus":' .. jsonString(photo.pickStatus) .. '}'
    end
    return '{"photos":[' .. table.concat(parts, ",") .. ']}'
end

LrTasks.startAsyncTask(function()
    local f = LrView.osFactory()
    local props = LrBinding.makePropertyTable(_PLUGIN)
    props.token = ""
    local result = LrDialogs.presentModalDialog({
        title = "MHPhotos: Send Selected Metadata",
        contents = f:column({
            bind_to_object = props,
            spacing = f:control_spacing(),
            f:static_text({ title = "Send up to 5 selected photo metadata records to localhost only." }),
            f:password_field({
                value = LrView.bind("token"),
                width_in_chars = 42,
            }),
            f:static_text({ title = "Paste the local MHPHOTOS_BRIDGE_TOKEN (not saved)." }),
        }),
        actionVerb = "Send",
    })
    if result ~= "ok" then
        return
    end
    local token = props.token
    if type(token) ~= "string" or #token < 32 or token:find("[\r\n]") then
        LrDialogs.message("MHPhotos", "Invalid local bridge token.", "warning")
        return
    end

    do
        local catalog = LrApplication.activeCatalog()
        if not catalog then
            error("No active catalog")
        end
        local selected = catalog:getTargetPhotos()
        if #selected == 0 then
            LrDialogs.message("MHPhotos", "Select at least one photo first.", "warning")
            return
        end
        if #selected > MAX_PHOTOS then
            LrDialogs.message(
                "MHPhotos",
                "More than 5 photos are selected. Please select at most 5 photos and retry.",
                "warning"
            )
            return
        end
        local photos = {}
        catalog:withReadAccessDo(function()
            for i = 1, #selected do
                local photo = selected[i]
                photos[#photos + 1] = {
                    fileName = value(photo, "fileName"),
                    rating = value(photo, "rating"),
                    pickStatus = value(photo, "pickStatus"),
                }
            end
        end)
        local body = encode(photos)
        local response, headers = LrHttp.post(
            URL, body,
            {
                { field = "Content-Type", value = "application/json" },
                { field = "Authorization", value = "Bearer " .. token },
            }
        )
        if not response or not headers or tonumber(headers.status) ~= 200 then
            error("Local bridge did not accept the request")
        end
        LrDialogs.message(
            "MHPhotos",
            string.format("Sent metadata for %d selected photos.", #photos),
            "info"
        )
    end
end)
