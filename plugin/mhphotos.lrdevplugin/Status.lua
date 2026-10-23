local LrDialogs = import 'LrDialogs'
local State = require 'State'
local Version = require 'Version'

local LrPathUtils = import 'LrPathUtils'
local fileText
do
  local f = io.open(LrPathUtils.child(LrPathUtils.child(LrPathUtils.getStandardFilePath('home'), '.mhphotos-lightroom-mcp'), 'plugin-status.txt'), 'r')
  if f then fileText = f:read('*a'); f:close() end
end

LrDialogs.message('MHPhotos MCP Bridge ' .. Version,
  'Plugin-Version: ' .. Version .. '\n' ..
  'Loop: ' .. (fileText or '(no status file yet)') .. '\n' ..
  'Status: ' .. tostring(State.status) ..
  '\nLast command: ' .. tostring(State.lastCommand or '-'), 'info')
