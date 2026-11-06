-- Tests Loop.lua (bridge polling loop) against fake LrHttp/LrTasks with a fake clock.
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

local now = 1000
os.time = function() return now end -- luacheck: ignore

local posts, script = {}, {}
local hangTasks = false
local sleepHook
local pending = {}
local State

function import(name)
  if name == 'LrHttp' then
    return { post = function(url, body, _, _, timeout)
      posts[#posts + 1] = { path = url:match('/v1/.*'), body = body, timeout = timeout }
      if url:find('/v1/result') then return '{"ok":true}', { status = 200 } end
      local nextStep = table.remove(script, 1)
      if not nextStep then State.stop = true; return '{"command":null}', { status = 200 } end
      return nextStep()
    end }
  end
  if name == 'LrTasks' then
    return {
      sleep = function(s) now = now + s; if sleepHook then sleepHook() end end,
      pcall = pcall,
      startAsyncTask = function(fn)
        if hangTasks then pending[#pending + 1] = fn else fn() end
      end,
    }
  end
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
    if p.path == '/v1/result' then out[#out + 1] = Json.decode(p.body) end
  end
  return out
end

-- 1) a normal command answers ok, an erroring command answers with its error, the loop goes on
Commands.fine = function() return { yes = true } end
Commands.broken = function() error('boom', 0) end
script = { command('fine', 30), command('broken', 30) }
Loop.run(0)
local r = results()
eq(#r, 2); eq(r[1].ok, true); eq(r[2].ok, false); eq(r[2].error, 'boom')

-- 2) a command that never comes back: answered as busy before the server's timeout, then polling goes on
posts, now, State.stop = {}, 1000, false
hangTasks = true
Commands.hangs = function() end
script = { command('hangs', 30), command('hangs', 120) }
Loop.run(0)
r = results()
eq(#r, 2, 'both hanging commands get an answer')
eq(r[1].ok, false); assert(r[1].error:find('^busy:'), r[1].error)
assert(r[2].error:find('117s'), r[2].error) -- answers 3 s before the server's 120 s
eq(posts[#posts].path, '/v1/poll', 'the loop polls again after a hanging command')
hangTasks, pending = false, {}

-- 3) an HTTP layer error does not end the loop
posts, State.stop = {}, false
script = { function() error('socket closed') end, command('fine', 30) }
Loop.run(0)
r = results()
eq(#r, 1); eq(r[1].ok, true)

-- 4) a loop of an older generation ends by itself
posts, State.stop = {}, false
State.generation = 3
script = { command('fine', 30) }
Loop.run(2)
eq(#posts, 0, 'an outdated loop does not poll')
State.generation = 0

-- 5) watchdog: a loop that stops beating is replaced by one of a new generation
State.stop, State.generation = false, 0
local started = {}
sleepHook = function() if now > 5000 then State.stop = true end end
Loop.watchdog(function(gen)
  started[#started + 1] = gen
  if gen == 0 then State.beat = now end -- the first loop beats once, then goes silent
end)
sleepHook = nil
eq(started[1], 0); eq(started[2], 1, 'a silent loop is replaced')
eq(State.generation, #started - 1)
-- a loop that keeps beating is left alone
State.stop, State.generation, now = false, 0, 1000
started = {}
sleepHook = function() State.beat = now; if now > 5000 then State.stop = true end end
Loop.watchdog(function(gen) started[#started + 1] = gen end)
sleepHook = nil
eq(#started, 1, 'a beating loop is not restarted')
print('loop ok')
