-- Long-poll loop: ask the MCP server for a command, run it, post the result.

local LrHttp = import 'LrHttp'
local LrTasks = import 'LrTasks'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'

local Json = require 'Json'
local State = require 'State'
local Commands = require 'Commands'
local Stats = require 'Stats'
local Version = require 'Version'

local Loop = {}
local stats = Stats.new(Version, os.time())

-- Watchdog (since 0.1.28). Lightroom calls can hang forever (a thumbnail whose callback never comes, a write
-- that waits on a busy catalog, createVirtualCopies). A hanging call used to keep the one polling task for good:
-- the server reported "not connected" until the plug-in was reloaded in the Plug-in Manager.
-- Now Init starts several RUNNER tasks. One of them is active and polls; the others only watch it. Each round
-- the active runner sets State.deadline (a healthy poll round ends within IDLE_GRACE, a command within the
-- timeout the server sent plus BUSY_GRACE). When the deadline passes, a watching runner takes over and polls
-- in its place. The hanging runner, once Lightroom lets it go, still posts its result and then becomes a
-- watcher again. No task is ever started from another task (see the note above execute()).
Loop.IDLE_GRACE = 60
Loop.BUSY_GRACE = 45
Loop.WATCH_EVERY = 10

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
local function isActive(name) return name == nil or State.active == name end

local function beat(seconds) State.deadline = os.time() + (seconds or Loop.IDLE_GRACE) end

local function mark(status, runner)
  -- a runner that lost its turn (it hung and another one took over) must not overwrite the active one's status
  if not isActive(runner) then return end
  State.status = status
  pcall(function()
    local home = LrPathUtils.getStandardFilePath('home')
    local dir = LrPathUtils.child(home, '.mhphotos-lightroom-mcp')
    local f = io.open(LrPathUtils.child(dir, 'plugin-status.txt'), 'w')
    if f then
      f:write(os.date('%Y-%m-%d %H:%M:%S ') .. status .. ' | last command: ' .. tostring(State.lastCommand or '-') .. '\n')
      f:close()
    end
    stats.status = status
    local j = io.open(LrPathUtils.child(dir, 'plugin-status.json'), 'w')
    if j then j:write(Json.encode(stats)); j:close() end
  end)
end

local function errorText(res)
  if type(res) == 'table' then
    return (res.code and (res.code .. ': ') or '') .. tostring(res.message)
  end
  return tostring(res)
end

-- The command runs INSIDE the loop task, as up to 0.1.21. 0.1.22 started every command in a task of its
-- own and waited for it; in Lightroom that task never finished (not even list_collections), so every
-- command came back "busy" after the deadline, while the polling task itself kept running. Why Lightroom
-- never completes a task started from inside the polling task is not known; plugin/tests/loop_test.lua
-- models exactly that, so commands must not depend on a second task. Keep the command in the polling task.
-- Lightroom's createVirtualCopies does not come back to its caller, and new copies read slowly for minutes. Since 0.1.25
-- Commands.create_virtual_copies only starts a job (its own task) and virtual_copy_job answers from memory; commands on
-- a new copy probe it for at most 8 s first (photo_busy). plugin/tests/virtual_copy_test.lua checks that no command
-- keeps this task longer than 10 s, whatever Lightroom does.
local function execute(cfg, command, runner)
  State.lastCommand = command.name
  beat((tonumber(command.timeout) or 30) + Loop.BUSY_GRACE)
  mark('running ' .. command.name, runner)
  local ok, res = LrTasks.pcall(Commands.run, command.name, command.args)
  local payload
  if ok then
    payload = { id = command.id, ok = true, data = res }
  else
    payload = { id = command.id, ok = false, error = errorText(res) }
  end
  Stats.finished(stats, command.name, payload.ok, payload.ok and '' or payload.error, os.time())
  local sent, encoded = pcall(Json.encode, payload)
  if not sent then
    payload = { id = command.id, ok = false, error = 'plugin could not encode the result: ' .. tostring(encoded) }
  end
  if isActive(runner) then beat() end
  post(cfg, '/v1/result', payload, 30)
  mark('connected (finished ' .. command.name .. ')', runner)
end

local function step(runner)
  beat()
  local cfg = readConfig()
  if not cfg then
    mark('no bridge.json (start the MCP server with MHPHOTOS_ADAPTER=bridge)', runner)
    LrTasks.sleep(3)
    return
  end
  mark('polling', runner)
  local body, hdrs = post(cfg, '/v1/poll', { client = 'lightroom' }, 20)
  local status = hdrs and hdrs.status
  if status == 200 and body then
    mark('connected', runner)
    local ok, msg = pcall(Json.decode, body)
    -- even a runner that lost its turn meanwhile runs a command it received: the server is waiting for it
    if ok and type(msg) == 'table' and type(msg.command) == 'table' then
      execute(cfg, msg.command, runner)
    end
  elseif status == 401 then
    mark('unauthorized (token changed?) - re-reading config', runner)
    LrTasks.sleep(2)
  else
    mark('MCP server unreachable (http status ' .. tostring(status) .. ')', runner)
    LrTasks.sleep(3)
  end
end

local function takeOver(runner)
  local from, cmd = State.active, State.lastCommand
  State.active = runner
  stats.takeovers = (stats.takeovers or 0) + 1
  stats.last_takeover_at = os.time()
  stats.last_takeover = tostring(from) .. ' hing bei ' .. tostring(cmd or '-')
  beat()
  mark('watchdog: ' .. tostring(from) .. ' did not come back from ' .. tostring(cmd or 'polling')
    .. ', ' .. runner .. ' polls now', runner)
end

-- One runner task (Init starts several). Returns when State.stop is set.
function Loop.runner(name)
  if State.active == nil then
    State.active = name
    mark('loop started', name)
  end
  while not State.stop do
    if State.active == name then
      -- an error anywhere in one round (config, HTTP, JSON) must not end the bridge
      local ok, err = LrTasks.pcall(step, name)
      if not ok then
        mark('loop error (continuing): ' .. errorText(err), name)
        LrTasks.sleep(2)
      end
    elseif os.time() > (State.deadline or 0) then
      takeOver(name)
    else
      LrTasks.sleep(Loop.WATCH_EVERY)
    end
  end
  State.status = 'stopped'
end

-- Single runner without a watchdog (tests, and plug-ins that start only one task).
function Loop.run() return Loop.runner('bridge') end

-- What Init calls: RUNNERS tasks, each a runner. Three, so that even two hanging Lightroom calls at once
-- leave one runner to poll.
Loop.RUNNERS = 3

return Loop
