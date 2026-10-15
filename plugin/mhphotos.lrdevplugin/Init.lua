local LrTasks = import 'LrTasks'
local State = require 'State'
local Loop = require 'Loop'

State.stop = false
LrTasks.startAsyncTask(function()
  Loop.run()
end)
