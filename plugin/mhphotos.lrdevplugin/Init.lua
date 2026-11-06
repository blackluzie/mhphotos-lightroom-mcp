local LrTasks = import 'LrTasks'
local LrPathUtils = import 'LrPathUtils'
local LrFileUtils = import 'LrFileUtils'

-- Init errors are otherwise invisible; leave a trace next to bridge.json.
local function trace(text)
  pcall(function()
    local dir = LrPathUtils.child(LrPathUtils.getStandardFilePath('home'), '.mhphotos-lightroom-mcp')
    LrFileUtils.createAllDirectories(dir)
    local f = io.open(LrPathUtils.child(dir, 'plugin-init.log'), 'a')
    if f then f:write(os.date('%Y-%m-%d %H:%M:%S ') .. text .. '\n'); f:close() end
  end)
end

trace('init started')
local ok, err = pcall(function()
  local State = require 'State'
  local Loop = require 'Loop'
  State.stop = false
  trace('init: modules loaded, starting loop task')
  LrTasks.startAsyncTask(function()
    local ok2, err2 = LrTasks.pcall(Loop.run)
    if not ok2 then trace('loop crashed: ' .. tostring(type(err2) == 'table' and err2.message or err2)) end
  end)
  -- Optional auto-export; does nothing unless ~/.mhphotos-lightroom-mcp/autoexport.json enables it.
  local AutoExport = require 'AutoExport'
  LrTasks.startAsyncTask(function()
    local ok3, err3 = LrTasks.pcall(AutoExport.loop)
    if not ok3 then trace('auto-export crashed: ' .. tostring(type(err3) == 'table' and err3.message or err3)) end
  end)
end)
if not ok then trace('init failed: ' .. tostring(err)) end
