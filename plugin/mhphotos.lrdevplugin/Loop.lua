-- Long-poll loop: ask the MCP server for a command, run it, post the result.

local LrHttp = import 'LrHttp'
local LrTasks = import 'LrTasks'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'

local Json = require 'Json'
local State = require 'State'
local Commands = require 'Commands'

local Loop = {}

local function readConfig()
  local home = LrPathUtils.getStandardFilePath('home')
  local path = LrPathUtils.child(LrPathUtils.child(home, '.mhphotos-lightroom-mcp'), 'bridge.json')
  if not LrFileUtils.exists(path) then return nil end
  local raw = LrFileUtils.readFile(path)
  if not raw then return nil end
  local ok, cfg = pcall(Json.decode, raw)
  if ok and type(cfg) == 'table' and cfg.url and cfg.token then return cfg end
  return nil
end

local function headers(token)
  return {
    { field = 'Authorization', value = 'Bearer ' .. token },
    { field = 'Content-Type', value = 'application/json' },
  }
end

local function post(cfg, path, payload, timeout)
  return LrHttp.post(cfg.url .. path, Json.encode(payload), headers(cfg.token), 'POST', timeout)
end

local function execute(cfg, command)
  State.lastCommand = command.name
  local ok, res = pcall(Commands.run, command.name, command.args)
  local payload
  if ok then
    payload = { id = command.id, ok = true, data = res }
  else
    local msg
    if type(res) == 'table' then
      msg = (res.code and (res.code .. ': ') or '') .. tostring(res.message)
    else
      msg = tostring(res)
    end
    payload = { id = command.id, ok = false, error = msg }
  end
  post(cfg, '/v1/result', payload, 30)
end

function Loop.run()
  while not State.stop do
    local cfg = readConfig()
    if not cfg then
      State.status = 'no bridge.json (start the MCP server with MHPHOTOS_ADAPTER=bridge)'
      LrTasks.sleep(3)
    else
      local body, hdrs = post(cfg, '/v1/poll', {}, 20)
      local status = hdrs and hdrs.status
      if status == 200 and body then
        State.status = 'connected'
        local ok, msg = pcall(Json.decode, body)
        if ok and type(msg) == 'table' and type(msg.command) == 'table' then
          execute(cfg, msg.command)
        end
      elseif status == 401 then
        State.status = 'unauthorized (token changed?) - re-reading config'
        LrTasks.sleep(2)
      else
        State.status = 'MCP server unreachable'
        LrTasks.sleep(3)
      end
    end
  end
  State.status = 'stopped'
end

return Loop
