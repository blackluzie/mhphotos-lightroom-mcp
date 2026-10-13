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

local MAX_PHOTOS = 50

local function shellQuote(s)
    return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

local SelectionMetadata = require "SelectionMetadata"

LrTasks.startAsyncTask(function()
    local installationPath = LrPathUtils.child(_PLUGIN.path, "Installation.lua")
    local installation = LrFileUtils.exists(installationPath) and require "Installation" or nil
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
                string.format("Please select no more than %d photos.", MAX_PHOTOS),
                "warning"
            )
            return
        end

        local photos = {}

        logger:info("Step 3: Reading metadata")
        catalog:withReadAccessDo(function()
            for _, photo in ipairs(selected) do
                photos[#photos + 1] = SelectionMetadata.read(photo)
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

        if installation and installation.profileRoot then
            privateDir = LrPathUtils.child(installation.profileRoot, "transfers")
        end
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

        local written, writeError = handle:write(SelectionMetadata.encode(photos))
        handle:close()

        if not written then
            error("Could not write transfer file: " .. tostring(writeError))
        end

        local command
        if LrFileUtils.exists(installationPath) then
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

        if installation and installation.profileRoot then
            if WIN_ENV then error("Mac test profile is macOS only") end
            command = "/bin/chmod 600 " .. shellQuote(filename)
                .. " && " .. shellQuote(installation.python)
                .. " -I -m mhphotos_lightroom_mcp.profile_runner "
                .. shellQuote(installation.profileRoot) .. " send " .. shellQuote(filename)
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
