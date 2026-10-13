-- Private file transport using the installed, secret-free Python path.
local LrTasks = import "LrTasks"
local LrFileUtils = import "LrFileUtils"
local LrPathUtils = import "LrPathUtils"
local Transfer = {}

local function shellQuote(text)
    return "'" .. tostring(text):gsub("'", "'\\''") .. "'"
end
local function windowsQuote(text)
    if text:find('["%%!&|<>^()\r\n]') then error("Unsafe transfer path") end
    return '"' .. text .. '"'
end

function Transfer.send(payload)
    local installationPath = LrPathUtils.child(_PLUGIN.path, "Installation.lua")
    if not LrFileUtils.exists(installationPath) then error("Updated installation required") end
    local installation = require "Installation"
    if type(installation.python) ~= "string" then error("Invalid installation") end
    local home = LrPathUtils.getStandardFilePath("home")
    if not home then error("No user home") end
    local directory = LrPathUtils.child(LrPathUtils.child(home, ".mhphotos-lightroom"), "transfers")
    if installation.profileRoot then
        directory = LrPathUtils.child(installation.profileRoot, "transfers")
        if not LrFileUtils.exists(directory) then error("Private profile transfers missing") end
    end
    if not LrFileUtils.exists(directory) then error("Private transfer directory missing") end
    local filename = LrPathUtils.child(directory,
        "selection-" .. tostring(os.time()) .. "-" .. tostring(math.random(100000,999999)) .. ".json")
    if LrFileUtils.exists(filename) then error("Transfer file exists") end
    local file = io.open(filename, "w")
    if not file then error("Cannot create transfer file") end
    local written = file:write(payload)
    file:close()
    if not written then LrFileUtils.delete(filename); error("Cannot write transfer file") end
    local ok, result = LrTasks.pcall(function()
        local command
        if WIN_ENV then
            command = '"' .. windowsQuote(installation.python)
                .. " -I -m mhphotos_lightroom_mcp.collection_sender " .. windowsQuote(filename) .. '"'
        else
            command = "/bin/chmod 600 " .. shellQuote(filename) .. " && "
                .. shellQuote(installation.python)
                .. " -I -m mhphotos_lightroom_mcp.collection_sender " .. shellQuote(filename)
        end
        if installation.profileRoot then
            if WIN_ENV then error("Mac test profile is macOS only") end
            command = "/bin/chmod 600 " .. shellQuote(filename) .. " && "
                .. shellQuote(installation.python)
                .. " -I -m mhphotos_lightroom_mcp.profile_runner "
                .. shellQuote(installation.profileRoot) .. " collections " .. shellQuote(filename)
        end
        return LrTasks.execute(command)
    end)
    if LrFileUtils.exists(filename) then LrFileUtils.delete(filename) end
    if not ok or result ~= 0 then error("Collection transfer failed") end
end
return Transfer
