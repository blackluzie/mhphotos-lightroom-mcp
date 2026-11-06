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

-- Seconds the loop waits for a command before it answers with an error and goes back to polling.
-- The server sends its own timeout; the answer must arrive a little before it gives up.
local function deadlineFor(command)
  local t = tonumber(command.timeout)
  if not t or t <= 0 then return 600 end
  return math.max(5, t - 3)
end

-- The command runs in its OWN task, never in the loop task: whatever a command does to its task
-- (Lightroom UI commands such as createVirtualCopies, a catalog call that never comes back), the loop
-- keeps beating, answers within the deadline and goes on polling.
local function execute(cfg, command, beat)
  State.lastCommand = command.name
  mark('running ' .. command.name)
  local job = { done = false }
  LrTasks.startAsyncTask(function()
    local ok, res = LrTasks.pcall(Commands.run, command.name, command.args)
    job.ok, job.res, job.done = ok, res, true
  end, 'MHPhotos: ' .. tostring(command.name))
  local started = os.time()
  local limit = deadlineFor(command)
  while not job.done and os.time() - started < limit and not State.stop do
    beat()
    LrTasks.sleep(0.1)
  end
  local payload
  if job.done and job.ok then
    payload = { id = command.id, ok = true, data = job.res }
  elseif job.done then
    payload = { id = command.id, ok = false, error = errorText(job.res) }
  else
    State.stuckCommand = command.name
    payload = { id = command.id, ok = false, error = 'busy: Lightroom is still working on ' .. tostring(command.name)
      .. ' after ' .. tostring(limit) .. 's; the bridge stays connected. Check the result in Lightroom before retrying.' }
  end
  Stats.finished(stats, command.name, payload.ok, payload.ok and '' or payload.error, os.time())
  local sent, encoded = pcall(Json.encode, payload)
  if not sent then
    payload = { id = command.id, ok = false, error = 'plugin could not encode the result: ' .. tostring(encoded) }
  end
  post(cfg, '/v1/result', payload, 30)
  mark('connected (finished ' .. command.name .. ')')
end

local function step(beat)
  local cfg = readConfig()
  if not cfg then
    mark('no bridge.json (start the MCP server with MHPHOTOS_ADAPTER=bridge)')
    LrTasks.sleep(3)
    return
  end
  mark('polling')
  local body, hdrs = post(cfg, '/v1/poll', { client = 'lightroom' }, 20)
  beat()
  local status = hdrs and hdrs.status
  if status == 200 and body then
    mark('connected')
    local ok, msg = pcall(Json.decode, body)
    if ok and type(msg) == 'table' and type(msg.command) == 'table' then
      execute(cfg, msg.command, beat)
    end
  elseif status == 401 then
    mark('unauthorized (token changed?) - re-reading config')
    LrTasks.sleep(2)
  else
    mark('MCP server unreachable (http status ' .. tostring(status) .. ')')
    LrTasks.sleep(3)
  end
end

-- generation: Init's watchdog starts a new loop when this one stops beating; an old loop that wakes up
-- again sees a newer generation and ends, so two loops never poll at the same time.
function Loop.run(generation)
  generation = generation or 0
  local function beat() State.beat = os.time() end
  local function current() return not State.stop and (State.generation or 0) == generation end
  beat()
  mark('loop started')
  while current() do
    -- an error anywhere in one round (config, HTTP, JSON) must not end the bridge
    local ok, err = LrTasks.pcall(step, beat)
    beat()
    if not ok then
      mark('loop error (continuing): ' .. errorText(err))
      LrTasks.sleep(2)
    end
  end
  if State.stop then State.status = 'stopped' end
end

-- Watchdog (own task, started by Init): if the loop has not beaten for STALE seconds (task gone or stuck
-- in a call that never returns), start a fresh loop with a new generation.
Loop.STALE = 90
function Loop.watchdog(start)
  State.generation = State.generation or 0
  start(State.generation)
  while not State.stop do
    LrTasks.sleep(10)
    if not State.stop and os.time() - (State.beat or os.time()) > Loop.STALE then
      State.generation = State.generation + 1
      State.restarts = (State.restarts or 0) + 1
      stats.restarts = State.restarts
      mark('loop restarted by watchdog (' .. tostring(State.restarts) .. 'x)')
      State.beat = os.time()
      start(State.generation)
    end
  end
end

return Loop
