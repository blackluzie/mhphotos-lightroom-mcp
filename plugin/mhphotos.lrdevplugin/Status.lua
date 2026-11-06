local LrDialogs = import 'LrDialogs'
local LrApplication = import 'LrApplication'
local LrPathUtils = import 'LrPathUtils'
local LrFileUtils = import 'LrFileUtils'
local Json = require 'Json'
local Version = require 'Version'
local StatusText = require 'StatusText'
local Webhook = require 'Webhook'

local dir = LrPathUtils.child(LrPathUtils.getStandardFilePath('home'), '.mhphotos-lightroom-mcp')

local function readJson(name)
  local path = LrPathUtils.child(dir, name)
  -- readFile raises an error for a missing file instead of returning nil
  if LrFileUtils.exists(path) ~= 'file' then return nil end
  local okRead, raw = pcall(LrFileUtils.readFile, path)
  if not okRead or not raw then return nil end
  local ok, value = pcall(Json.decode, raw)
  return ok and value or nil
end

local info = readJson('plugin-status.json')
local hookCount
local raw = readJson('webhooks.json')
if raw then hookCount = #Webhook.names((Webhook.normalize(raw))) end
local catalogPath
pcall(function() catalogPath = LrApplication.activeCatalog():getPath() end)

LrDialogs.message('MHPhotos MCP Bridge ' .. Version,
  StatusText.format(info, { catalog = catalogPath, hooks = hookCount, now = os.time() }), 'info')
