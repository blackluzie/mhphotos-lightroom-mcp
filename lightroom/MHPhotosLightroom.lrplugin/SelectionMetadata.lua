-- A fixed, read-only metadata projection. Never enumerate all photo metadata.
local LrTasks = import "LrTasks"
local Metadata = {}

local baseFields = { "fileName", "rating", "pickStatus" }
local formattedFields = {
    "cameraMake", "cameraModel", "lens", "shutterSpeed", "aperture",
    "isoSpeedRating", "focalLength", "dimensions", "croppedDimensions", "fileType",
}
local allFields = { "fileName", "rating", "pickStatus", "dateTimeOriginalISO8601" }
for _, key in ipairs(formattedFields) do allFields[#allFields + 1] = key end

local function normalize(value)
    if type(value) ~= "string" and type(value) ~= "number" then return nil end
    local text = tostring(value):gsub("[%c]", " ")
    -- Limit bytes without cutting a UTF-8 code point in half.
    local finish = math.min(#text, 80)
    while finish > 0 and finish < #text do
        local nextByte = string.byte(text, finish + 1)
        if nextByte < 128 or nextByte >= 192 then break end
        finish = finish - 1
    end
    return text:sub(1, finish)
end

local function optional(photo, key, formatted)
    -- LrTasks.pcall permits SDK getters to yield, unlike Lua's regular pcall.
    local ok, result = LrTasks.pcall(function()
        if formatted then return photo:getFormattedMetadata(key) end
        return photo:getRawMetadata(key)
    end)
    if not ok then return nil end
    result = normalize(result)
    if result == "" then return nil end
    return result
end

function Metadata.read(photo)
    local result = {}
    for _, key in ipairs(baseFields) do
        local value
        if key == "fileName" then value = photo:getFormattedMetadata(key)
        else value = photo:getRawMetadata(key) end
        result[key] = normalize(value) or ""
    end
    result.dateTimeOriginalISO8601 = optional(photo, "dateTimeOriginalISO8601", false)
    for _, key in ipairs(formattedFields) do result[key] = optional(photo, key, true) end
    return result
end

local function jsonString(text)
    return '"' .. text:gsub('[%z\1-\31\\"]', function(c)
        local escapes = { ['\\'] = '\\\\', ['"'] = '\\"', ['\n'] = '\\n',
                          ['\r'] = '\\r', ['\t'] = '\\t' }
        return escapes[c] or string.format("\\u%04x", string.byte(c))
    end) .. '"'
end

function Metadata.encode(photos)
    local records = {}
    for _, photo in ipairs(photos) do
        local fields = {}
        for _, key in ipairs(allFields) do
            if photo[key] ~= nil then
                fields[#fields + 1] = jsonString(key) .. ":" .. jsonString(photo[key])
            end
        end
        records[#records + 1] = "{" .. table.concat(fields, ",") .. "}"
    end
    return '{"photos":[' .. table.concat(records, ",") .. ']}'
end
return Metadata
