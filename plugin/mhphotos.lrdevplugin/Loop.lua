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

-- State lives in a module table, but menu scripts (Status) run in their own Lua environment and
-- may not see it, so every status change is also written to a file next to bridge.json.
local function mark(status)
  State.status = status
  pcall(function()
    local home = LrPathUtils.getStandardFilePath('home')
    local f = io.open(LrPathUtils.child(LrPathUtils.child(home, '.mhphotos-lightroom-mcp'), 'plugin-status.txt'), 'w')
    if f then
      f:write(os.date('%Y-%m-%d %H:%M:%S ') .. status .. ' | last command: ' .. tostring(State.lastCommand or '-') .. '\n')
      f:close()
    end
  end)
end

local function execute(cfg, command)
  State.lastCommand = command.name
  mark('running ' .. command.name)
  local ok, res = LrTasks.pcall(Commands.run, command.name, command.args)
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
  mark('connected (finished ' .. command.name .. ')')
end

function Loop.run()
  mark('loop started')
  while not State.stop do
    local cfg = readConfig()
    if not cfg then
      mark('no bridge.json (start the MCP server with MHPHOTOS_ADAPTER=bridge)')
      LrTasks.sleep(3)
    else
      mark('polling')
      local body, hdrs = post(cfg, '/v1/poll', { client = 'lightroom' }, 20)
      local status = hdrs and hdrs.status
      if status == 200 and body then
        mark('connected')
        local ok, msg = pcall(Json.decode, body)
        if ok and type(msg) == 'table' and type(msg.command) == 'table' then
          execute(cfg, msg.command)
        end
      elseif status == 401 then
        mark('unauthorized (token changed?) - re-reading config')
        LrTasks.sleep(2)
      else
        mark('MCP server unreachable (http status ' .. tostring(status) .. ')')
        LrTasks.sleep(3)
      end
    end
  end
  State.status = 'stopped'
end

return Loop
