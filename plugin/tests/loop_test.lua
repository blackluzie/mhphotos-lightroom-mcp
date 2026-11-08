-- Tests Loop.lua (bridge polling loop) against fake LrHttp/LrTasks with a fake clock.
--
-- The fake LrTasks schedules cooperatively, like Lightroom: every task is a coroutine,
-- startAsyncTask only queues it (it runs once the current task gives up control), and only
-- LrTasks.sleep/yield and yielding SDK calls (LrHttp.post) hand control to the next task that is due.
-- The earlier fake ran each task synchronously inside startAsyncTask, which hid the 0.1.22 regression:
-- in Lightroom a command task started from the polling task never finished, so every command,
-- reads included, came back "busy".
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

local now = 1000
os.time = function() return now end -- luacheck: ignore

-- ------------------------------------------------------------ cooperative LrTasks
local tasks = {}  -- queued/sleeping tasks: { co, wake, name }
local current     -- the task running right now (nil = test code outside any task)
-- Models what 0.1.22 ran into in Lightroom: a task started from inside another task is queued but
-- never runs to the end.
local nestedTasksStall = false

-- pcall that lets the protected function yield (Lua 5.1's pcall cannot; Lightroom's LrTasks.pcall can)
local function copcall(f, ...)
  local n, args = select('#', ...), { ... }
  local co = coroutine.create(function() return f(unpack(args, 1, n)) end)
  local function resumed(ok, ...)
    if not ok then return false, (...) end
    if coroutine.status(co) == 'dead' then return true, ... end
    return resumed(coroutine.resume(co, coroutine.yield(...)))
  end
  return resumed(coroutine.resume(co))
end

local Tasks = {}
function Tasks.startAsyncTask(fn, name)
  local wake = now
  if current and nestedTasksStall then wake = math.huge end
  tasks[#tasks + 1] = { co = coroutine.create(fn), wake = wake, name = name }
end
function Tasks.sleep(s)
  assert(current, 'LrTasks.sleep called outside a task')
  coroutine.yield(s or 0)
end
function Tasks.yield() Tasks.sleep(0) end
Tasks.pcall = copcall

-- Runs the due task with the earliest wake time (the resumed one goes to the back of the queue, so
-- tasks waking at the same moment take turns) until no task is due before `untilTime`.
local function runTasks(untilTime)
  while true do
    local best
    for i, t in ipairs(tasks) do
      if not best or t.wake < tasks[best].wake then best = i end
    end
    if not best or tasks[best].wake == math.huge or tasks[best].wake > untilTime then return end
    local t = table.remove(tasks, best)
    if t.wake > now then now = t.wake end
    current = t
    local ok, s = coroutine.resume(t.co)
    current = nil
    if not ok then error('task ' .. tostring(t.name) .. ' crashed: ' .. tostring(s), 0) end
    if coroutine.status(t.co) ~= 'dead' then
      t.wake = now + (tonumber(s) or 0)
      tasks[#tasks + 1] = t
    end
  end
end

-- ------------------------------------------------------------ fake HTTP server
local posts, script = {}, {}
local State

function import(name)
  if name == 'LrHttp' then
    return { post = function(url, body, _, _, timeout)
      Tasks.yield() -- LrHttp.post yields in Lightroom
      posts[#posts + 1] = { path = url:match('/v1/.*'), body = body, timeout = timeout, at = now }
      if url:find('/v1/result') then return '{"ok":true}', { status = 200 } end
      local nextStep = table.remove(script, 1)
      if not nextStep then State.stop = true; return '{"command":null}', { status = 200 } end
      return nextStep()
    end }
  end
  if name == 'LrTasks' then return Tasks end
  if name == 'LrFileUtils' then
    return { exists = function() return true end, readFile = function() return '{"url":"http://x","token":"t"}' end }
  end
  if name == 'LrPathUtils' then
    return { getStandardFilePath = function() return '/nonexistent-mhphotos-test' end,
      child = function(a, b) return a .. '/' .. b end }
  end
  error('unexpected import ' .. name)
end

State = require 'State'
-- Commands.lua needs the whole Lightroom API; the loop only needs Commands.run
local Commands = {}
function Commands.run(name, args) return Commands[name](args) end
package.loaded['Commands'] = Commands
local Loop = require 'Loop'
local Json = require 'Json'

local function command(name, timeout)
  return function()
    return Json.encode({ command = { id = name .. '-id', name = name, args = {}, timeout = timeout } }), { status = 200 }
  end
end

local function results()
  local out = {}
  for _, p in ipairs(posts) do
    if p.path == '/v1/result' then
      local r = Json.decode(p.body); r.at = p.at; out[#out + 1] = r
    end
  end
  return out
end

-- start the loop as Init does (in its own task) and let the scheduler run for up to an hour of fake time
local function runLoop(steps)
  posts, script, State.stop, tasks = {}, steps, false, {}
  Tasks.startAsyncTask(function()
    local ok, err = Tasks.pcall(Loop.run)
    if not ok then error('loop crashed: ' .. tostring(err), 0) end
  end, 'MHPhotos: bridge loop')
  local started = now
  runTasks(now + 3600)
  return started
end

-- 1) a normal command answers ok, an erroring command answers with its error, the loop goes on
Commands.fine = function() return { yes = true } end
Commands.broken = function() error('boom', 0) end
runLoop({ command('fine', 30), command('broken', 30) })
local r = results()
eq(#r, 2); eq(r[1].ok, true); eq(r[2].ok, false); eq(r[2].error, 'boom')
eq(State.status, 'stopped')

-- 2) REGRESSION 0.1.22: in Lightroom a task started from the polling task never finished, so even
-- list_collections came back "busy" after 26 s. A read must answer at once, without needing a second task.
nestedTasksStall = true
Commands.list_collections = function() return { collections = { { id = 7, name = 'Kreuzfahrt 2026' } } } end
Commands.get_photo = function() return { id = 'u1' } end
local t0 = runLoop({ command('list_collections', 29), command('get_photo', 29) })
r = results()
eq(#r, 2, 'both reads get an answer')
eq(r[1].ok, true, 'list_collections answers ok, not busy: ' .. tostring(r[1].error))
eq(r[1].data.collections[1].name, 'Kreuzfahrt 2026')
eq(r[2].ok, true, 'get_photo answers ok, not busy: ' .. tostring(r[2].error))
assert(r[2].at - t0 < 1, 'reads answer at once, took ' .. tostring(r[2].at - t0) .. 's')
nestedTasksStall = false

-- 3) a command that yields (sleeps, like makeCopy waiting for the new copy) still answers, and other
-- plug-in tasks (auto-export, publish) keep running meanwhile
local ticks = 0
Commands.slow = function() Tasks.sleep(2); Tasks.sleep(2); return { done = true } end
posts, script, State.stop, tasks = {}, { command('slow', 30), command('fine', 30) }, false, {}
Tasks.startAsyncTask(function() while not State.stop do ticks = ticks + 1; Tasks.sleep(1) end end, 'other')
Tasks.startAsyncTask(function() Tasks.pcall(Loop.run) end, 'MHPhotos: bridge loop')
runTasks(now + 3600)
r = results()
eq(#r, 2); eq(r[1].ok, true); eq(r[1].data.done, true); eq(r[2].ok, true)
assert(ticks >= 3, 'other tasks run while a command sleeps (ticks ' .. ticks .. ')')

-- 4) an HTTP layer error does not end the loop
runLoop({ function() error('socket closed') end, command('fine', 30) })
r = results()
eq(#r, 1); eq(r[1].ok, true)

-- 5) a result that cannot be encoded still gets an answer (the server would otherwise wait in vain)
Commands.weird = function() return { f = function() end } end
runLoop({ command('weird', 30) })
r = results()
eq(#r, 1); eq(r[1].ok, false); assert(r[1].error:find('could not encode'), r[1].error)

-- 6) the fake itself: a nested task really stalls when nestedTasksStall is set (so test 2 means something)
nestedTasksStall, tasks = true, {}
local childDone = false
Tasks.startAsyncTask(function() Tasks.startAsyncTask(function() childDone = true end) end, 'parent')
runTasks(now + 3600)
eq(childDone, false, 'nested task stalls in the fake')
nestedTasksStall, tasks = false, {}
Tasks.startAsyncTask(function() Tasks.startAsyncTask(function() childDone = true end) end, 'parent')
runTasks(now + 3600)
eq(childDone, true, 'nested task runs normally otherwise')

-- 7) WATCHDOG (0.1.28): a command that never comes back (a thumbnail callback that never fires) used to keep
-- the only polling task forever; the server said "not connected" until the plug-in was reloaded. With several
-- runners, a watching one takes over after the command's timeout plus the grace time and the next command is
-- answered. The hanging runner, once released, posts its own result and does not poll in parallel.
local release = false
Commands.hang = function()
  while not release do Tasks.sleep(5) end
  return { late = true }
end
local function idlePoll() Tasks.sleep(10); return '{"command":null}', { status = 200 } end
local steps = { command('hang', 30), command('fine', 30) }
for _ = 1, 30 do steps[#steps + 1] = idlePoll end -- about 5 minutes of quiet polling afterwards
local scripted = #steps
posts, script, State.stop, tasks, State.active, State.deadline = {}, steps, false, {}, nil, 0
for i = 1, 2 do
  local name = 'runner-' .. i
  Tasks.startAsyncTask(function() Tasks.pcall(Loop.runner, name) end, name)
end
local w0 = now
-- Lightroom lets the hanging call go after 3 minutes
Tasks.startAsyncTask(function() Tasks.sleep(180); release = true end, 'lightroom')
runTasks(now + 3600)
r = results()
eq(#r, 2, 'both commands get an answer')
eq(r[1].id, 'fine-id', 'the command after the hanging one is answered first')
local took = r[1].at - w0
assert(took >= 30 + Loop.BUSY_GRACE and took <= 30 + Loop.BUSY_GRACE + Loop.WATCH_EVERY + 5,
  'takeover after timeout + grace, took ' .. took .. ' s')
eq(r[2].id, 'hang-id', 'the hanging runner still posts its result once Lightroom lets it go')
eq(r[2].data.late, true)
eq(State.active, 'runner-2', 'the released runner does not take the turn back')
local polls = 0
for _, p in ipairs(posts) do if p.path == '/v1/poll' then polls = polls + 1 end end
eq(polls, scripted + 1, 'every scripted poll was used once (no parallel polling)')

-- 8) with nothing hanging, the watchers never take over (a quiet bridge polls for an hour without a takeover)
posts, State.stop, tasks, State.active, State.deadline = {}, false, {}, nil, 0
local idle = {}
for _ = 1, 150 do idle[#idle + 1] = function() return '{"command":null}', { status = 200 } end end
script = idle
for i = 1, 3 do
  local name = 'runner-' .. i
  Tasks.startAsyncTask(function() Tasks.pcall(Loop.runner, name) end, name)
end
runTasks(now + 3600)
eq(State.active, 'runner-1', 'no takeover while the active runner keeps polling')

print('loop ok')
