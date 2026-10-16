-- Library > Plug-in Extras > MHPhotos MCP: Selbsttest
-- Read-only check of every read command; writes the report next to bridge.json.
local LrTasks = import 'LrTasks'
local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'

LrTasks.startAsyncTask(function()
  local Commands = require 'Commands'
  local Json = require 'Json'
  local ok, report = LrTasks.pcall(Commands.selftest)
  if not ok then
    LrDialogs.message('MHPhotos MCP Selbsttest', 'Selbsttest abgebrochen: ' .. tostring(report), 'critical')
    return
  end
  local lines = { report.environment, '' }
  for _, c in ipairs(report.checks) do
    lines[#lines + 1] = (c.ok and 'OK   ' or 'FEHLER ') .. c.name .. ' - ' .. c.detail
  end
  local text = table.concat(lines, '\n')
  local dir = LrPathUtils.child(LrPathUtils.getStandardFilePath('home'), '.mhphotos-lightroom-mcp')
  LrFileUtils.createAllDirectories(dir)
  local f = io.open(LrPathUtils.child(dir, 'selftest.json'), 'w')
  if f then f:write(Json.encode(report)); f:close() end
  LrDialogs.message('MHPhotos MCP Selbsttest: ' .. report.passed .. ' ok, ' .. report.failed .. ' Fehler',
    text, report.failed == 0 and 'info' or 'warning')
end)
