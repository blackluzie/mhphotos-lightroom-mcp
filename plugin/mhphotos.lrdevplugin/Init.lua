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
  State.active = nil
  trace('init: modules loaded, starting ' .. Loop.RUNNERS .. ' bridge runner tasks (watchdog)')
  -- All runners are started here, never from inside another task (in Lightroom such nested tasks stalled,
  -- see Loop.lua). If the polling runner hangs in a Lightroom call, another one takes over.
  for i = 1, Loop.RUNNERS do
    local name = 'runner-' .. i
    LrTasks.startAsyncTask(function()
      local ok2, err2 = LrTasks.pcall(Loop.runner, name)
      if not ok2 then trace(name .. ' crashed: ' .. tostring(type(err2) == 'table' and err2.message or err2)) end
      -- a crashed runner gives up its turn, so a watching one takes over at once
      if State.active == name then State.deadline = 0 end
    end, 'MHPhotos: bridge ' .. name)
  end
  -- Optional auto-export; does nothing unless ~/.mhphotos-lightroom-mcp/autoexport.json enables it.
  local AutoExport = require 'AutoExport'
  LrTasks.startAsyncTask(function()
    local ok3, err3 = LrTasks.pcall(AutoExport.loop)
    if not ok3 then trace('auto-export crashed: ' .. tostring(type(err3) == 'table' and err3.message or err3)) end
  end)
end)
if not ok then trace('init failed: ' .. tostring(err)) end
