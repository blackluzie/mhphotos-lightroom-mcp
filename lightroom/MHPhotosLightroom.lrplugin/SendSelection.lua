-- Explicit, read-only transfer of selected Lightroom metadata.
-- Authentication is handled by the local Python helper.

local LrApplication = import "LrApplication"
local LrDialogs = import "LrDialogs"
local LrTasks = import "LrTasks"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"

local LrLogger = import "LrLogger"

local logger = LrLogger("MHPhotosSelection")
logger:enable("logfile")

local MAX_PHOTOS = 5

local function shellQuote(s)
    return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

local function value(photo, key)
    local result

    if key == "fileName" then
        result = photo:getFormattedMetadata(key)
    else
        result = photo:getRawMetadata(key)
    end

    if type(result) ~= "string" and type(result) ~= "number" then
        return ""
    end

    return tostring(result):gsub("[%c]", " "):sub(1, 80)
end

local function jsonString(s)
    return '"' .. s:gsub('[%z\1-\31\\"]', function(c)
        local escapes = {
            ['\\'] = '\\\\',
            ['"'] = '\\"',
            ['\b'] = '\\b',
            ['\f'] = '\\f',
            ['\n'] = '\\n',
            ['\r'] = '\\r',
            ['\t'] = '\\t',
        }
        return escapes[c] or string.format("\\u%04x", string.byte(c))
    end) .. '"'
end

local function encode(photos)
    local parts = {}

    for _, photo in ipairs(photos) do
        parts[#parts + 1] =
            '{"fileName":' .. jsonString(photo.fileName)
            .. ',"rating":' .. jsonString(photo.rating)
            .. ',"pickStatus":' .. jsonString(photo.pickStatus)
            .. '}'
    end

    return '{"photos":[' .. table.concat(parts, ",") .. ']}'
end

LrTasks.startAsyncTask(function()
    logger:info("SendSelection started")
        logger:info("Step 1: Getting catalog")
        local catalog = LrApplication.activeCatalog()

        if not catalog then
            error("No active Lightroom catalog")
        end

        logger:info("Step 2: Getting selected photos")
        local selected = catalog:getTargetPhotos()

        if #selected == 0 then
            LrDialogs.message(
                "MHPhotos",
                "Please select at least one photo.",
                "warning"
            )
            return
        end

        if #selected > MAX_PHOTOS then
            LrDialogs.message(
                "MHPhotos",
                "Please select no more than 5 photos.",
                "warning"
            )
            return
        end

        local photos = {}

        logger:info("Step 3: Reading metadata")
        catalog:withReadAccessDo(function()
            for _, photo in ipairs(selected) do
                photos[#photos + 1] = {
                    fileName = value(photo, "fileName"),
                    rating = value(photo, "rating"),
                    pickStatus = value(photo, "pickStatus"),
                }
            end
        end)

        logger:info("Step 4: Preparing transfer directory")
        local documents = LrPathUtils.getStandardFilePath("documents")
        if not documents then
            error("Cannot determine Lightroom documents directory")
        end

        local home = LrPathUtils.getStandardFilePath("home") or LrPathUtils.parent(documents)
        if not home then
            error("Cannot determine user home directory")
        end
        local privateDir = LrPathUtils.child(
            LrPathUtils.child(home, ".mhphotos-lightroom"),
            "transfers"
        )

        if not LrFileUtils.exists(privateDir) then
            error("Private transfer directory is missing")
        end

        local filename = LrPathUtils.child(
            privateDir,
            "selection-" .. tostring(os.time())
                .. "-" .. tostring(math.random(100000, 999999))
                .. ".json"
        )

        if LrFileUtils.exists(filename) then
            error("Transfer filename already exists")
        end

        logger:info("Step 5: Writing JSON")
        if not io or type(io.open) ~= "function" then
            error("Lua file writing is unavailable in Lightroom")
        end

        local handle, openError = io.open(filename, "w")
        if not handle then
            error("Could not open transfer file: " .. tostring(openError))
        end

        local written, writeError = handle:write(encode(photos))
        handle:close()

        if not written then
            error("Could not write transfer file: " .. tostring(writeError))
        end

        local installationPath = LrPathUtils.child(_PLUGIN.path, "Installation.lua")
        local command
        if LrFileUtils.exists(installationPath) then
            local installation = dofile(installationPath)
            if type(installation) ~= "table" or type(installation.python) ~= "string" then
                error("Invalid MHPhotos installation")
            end
            if WIN_ENV then
                local function windowsQuote(path)
                    if path:find('["%%!&|<>^()\r\n]') then
                        error("Unsupported character in Windows transfer path")
                    end
                    return '"' .. path .. '"'
                end
                command = windowsQuote(installation.python)
                    .. " -I -m mhphotos_lightroom_mcp.selection_sender "
                    .. windowsQuote(filename)
                -- Lightroom SDK requires enclosing the entire Windows command.
                command = '"' .. command .. '"'
            else
                command = "/bin/chmod 600 " .. shellQuote(filename)
                    .. " && " .. shellQuote(installation.python)
                    .. " -I -m mhphotos_lightroom_mcp.selection_sender "
                    .. shellQuote(filename)
            end
        else
            -- Preserve the existing working macOS helper installation.
            if WIN_ENV then
                error("Please install MHPhotos for Windows first")
            end
            local launcher = LrPathUtils.child(
                LrPathUtils.child(home, ".local"), "bin/mhphotos-lightroom-send"
            )
            command = "/bin/chmod 600 " .. shellQuote(filename)
                .. " && /bin/test -f " .. shellQuote(filename)
                .. " && /bin/sh " .. shellQuote(launcher)
                .. " " .. shellQuote(filename)
        end

        logger:info("Starting local helper")
        local exitCode = LrTasks.execute(command)
        logger:info("Helper exit code: " .. tostring(exitCode))

        if exitCode ~= 0 then
            -- The Python helper normally removes accepted input files.
            -- Clean up this file if execution failed earlier.
            if LrFileUtils.exists(filename) then
                LrFileUtils.delete(filename)
            end

            error("Local transfer failed (exit code "
                .. tostring(exitCode) .. ")")
        end

        LrDialogs.message(
            "MHPhotos",
            string.format(
                "Successfully sent metadata for %d selected photo(s).",
                #photos
            ),
            "info"
        )
end)
