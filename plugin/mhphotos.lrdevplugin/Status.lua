local LrDialogs = import 'LrDialogs'
local State = require 'State'

LrDialogs.message('MHPhotos MCP Bridge',
  'Status: ' .. tostring(State.status) ..
  '\nLast command: ' .. tostring(State.lastCommand or '-'), 'info')
