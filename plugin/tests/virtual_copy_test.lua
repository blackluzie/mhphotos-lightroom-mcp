-- Tests create_virtual_copies through the REAL Loop.lua and Commands.lua against a cooperative LrTasks fake
-- (the same scheduler as loop_test.lua: tasks are coroutines, startAsyncTask only queues, sleep/yield and
-- LrHttp.post hand over control).
--
-- The Lightroom behaviour modelled here (06.11.2026, plug-in 0.1.21 and 0.1.23): catalog:createVirtualCopies
-- makes the copy but never comes back to the calling task. 0.1.23 called it in the polling task, so the bridge
-- stayed on "running create_virtual_copies" until Lightroom was restarted. Since 0.1.24 the call runs in a
-- helper task that is never waited for; the command watches the original's virtualCopies for a bounded time.
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

local now = 1000
os.time = function() return now end -- luacheck: ignore

-- ------------------------------------------------------------ cooperative LrTasks (as in loop_test.lua)
local tasks = {}
local current
local nestedTasksStall = false -- a task started from inside another task never runs (what 0.1.22 ran into)

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

-- ------------------------------------------------------------ fake catalog
local function newPhoto(uuid)
  local p = { raw = { uuid = uuid, virtualCopies = {} } }
  function p:getRawMetadata(k) return self.raw[k] end
  return p
end
local photos = { newPhoto('u1'), newPhoto('u2') }
local selected
-- how createVirtualCopies behaves: 'returns' (like the SDK says), 'hangs' (makes the copy, never comes back,
-- like Lightroom Classic), 'late' (comes back and copies only after 40 s)
local copyMode = 'returns'
local copyCalls = 0

local function makeCopyOfSelection(copyName)
  local master = selected
  local copy = newPhoto('copy' .. (#photos + 1) .. '-of-' .. master.raw.uuid)
  copy.raw.isVirtualCopy, copy.raw.masterPhoto, copy.raw.copyName = true, master, copyName
  photos[#photos + 1] = copy
  master.raw.virtualCopies[#master.raw.virtualCopies + 1] = copy
  return copy
end

local fakeCatalog = {
  findPhotoByUuid = function(_, id) for _, p in ipairs(photos) do if p.raw.uuid == id then return p end end end,
  setSelectedPhotos = function(_, p) selected = p end,
  getTargetPhoto = function() return selected end,
  setActiveSources = function() return true end,
  getChildCollections = function() return { { localIdentifier = 7, getName = function() return 'Kreuzfahrt 2026' end } } end,
  getChildCollectionSets = function() return {} end,
  withWriteAccessDo = function(_, _, fn) fn(); return 'executed' end,
  createVirtualCopies = function(_, copyName)
    copyCalls = copyCalls + 1
    if copyMode == 'late' then Tasks.sleep(40) end
    local copy = makeCopyOfSelection(copyName)
    if copyMode == 'hangs' then Tasks.sleep(math.huge) end -- never comes back to the caller
    return { copy }
  end,
}
local catalogProxy = setmetatable({ kAllPhotos = 'kAllPhotos' }, { __index = function(_, k)
  local f = fakeCatalog[k]
  if f == nil then return nil end
  return function(self, ...) return f(self, ...) end end })

-- ------------------------------------------------------------ fake HTTP server + imports
local posts, script = {}, {}
local State

function import(name)
  if name == 'LrHttp' then
    return { post = function(url, body, _, _, timeout)
      Tasks.yield()
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
  if name == 'LrApplication' then return { activeCatalog = function() return catalogProxy end } end
  if name == 'LrApplicationView' then return { switchToModule = function() end, gridView = function() end } end
  if name == 'LrDate' then return { currentTime = function() return now end } end
  if name == 'LrStringUtils' then return {} end
  error('unexpected import ' .. name)
end

State = require 'State'
local Loop = require 'Loop'
local Json = require 'Json'

local function command(name, args)
  return function()
    return Json.encode({ command = { id = name .. '-id', name = name, args = args or {} } }), { status = 200 }
  end
end

local function runLoop(steps)
  posts, script, State.stop = {}, steps, false
  Tasks.startAsyncTask(function()
    local ok, err = Tasks.pcall(Loop.run)
    if not ok then error('loop crashed: ' .. tostring(err), 0) end
  end, 'MHPhotos: bridge loop')
  local started = now
  runTasks(now + 3600)
  local out = {}
  for _, p in ipairs(posts) do
    if p.path == '/v1/result' then
      local r = Json.decode(p.body); r.at = p.at; out[#out + 1] = r
    end
  end
  return out, started
end

local function copyOf(uuid) return command('create_virtual_copies', { photo_ids = { uuid }, copy_name = 'Insta 4:5' }) end

-- 1) REGRESSION 0.1.23: Lightroom makes the copy but createVirtualCopies never returns. The command still answers
-- with the copy id at once, and the next command is answered too (the bridge stays connected).
copyMode = 'hangs'
local r, t0 = runLoop({ copyOf('u1'), command('list_collections') })
eq(#r, 2, 'both commands get an answer')
eq(r[1].ok, true, 'create_virtual_copies answers ok: ' .. tostring(r[1].error))
eq(r[1].data.copies[1].source_id, 'u1')
eq(r[1].data.copies[1].id, photos[1].raw.virtualCopies[1].raw.uuid)
eq(photos[1].raw.virtualCopies[1].raw.copyName, 'Insta 4:5')
assert(r[1].at - t0 <= 2, 'the copy answer comes at once, took ' .. tostring(r[1].at - t0) .. 's')
eq(r[2].ok, true, 'list_collections after the copy answers: ' .. tostring(r[2].error))
eq(r[2].data.collections[1].name, 'Kreuzfahrt 2026')
eq(State.status, 'stopped', 'the loop ended normally (script empty), it was not stuck')

-- 2) as the copy was seen, the hanging call does not block the next copy; two photos in one call work too
r = runLoop({ command('create_virtual_copies', { photo_ids = { 'u1', 'u2' } }) })
eq(r[1].ok, true, tostring(r[1].error))
eq(#r[1].data.copies, 2)
eq(r[1].data.copies[1].source_id, 'u1'); eq(r[1].data.copies[2].source_id, 'u2')
eq(#photos[1].raw.virtualCopies, 2); eq(#photos[2].raw.virtualCopies, 1)
eq(r[1].data.copies[2].id, photos[2].raw.virtualCopies[1].raw.uuid)

-- 3) a Lightroom that returns normally still works (copy taken from the return value or virtualCopies)
copyMode = 'returns'
r = runLoop({ copyOf('u2') })
eq(r[1].ok, true, tostring(r[1].error)); eq(r[1].data.copies[1].id, photos[2].raw.virtualCopies[2].raw.uuid)

-- 4) the helper task never runs (as with 0.1.22's nested tasks): a clear timeout error after about 20 s, with the
-- manual way out, the bridge keeps answering, and no further copy is started while that call is still open
nestedTasksStall = true
local callsBefore, nBefore = copyCalls, #photos
r, t0 = runLoop({ copyOf('u1'), command('list_collections'), copyOf('u2') })
eq(#r, 3, 'every command gets an answer')
eq(r[1].ok, false)
assert(r[1].error:find('virtual_copy_timeout'), r[1].error)
assert(r[1].error:find("Cmd+'", 1, true), 'error names the manual way: ' .. r[1].error)
assert(r[1].error:find('virtual_copies=false', 1, true), r[1].error)
assert(r[1].at - t0 >= 20 and r[1].at - t0 <= 25, 'bounded wait, took ' .. tostring(r[1].at - t0) .. 's')
eq(r[2].ok, true, 'list_collections answers after the timeout')
eq(r[3].ok, false); assert(r[3].error:find('copy_pending'), r[3].error)
eq(copyCalls, callsBefore, 'no createVirtualCopies ran'); eq(#photos, nBefore, 'no copy was made')
nestedTasksStall = false
-- if Lightroom runs the stalled call after all, it copies u1 (nothing changed the selection meanwhile) and the
-- block is lifted
for _, t in ipairs(tasks) do t.wake = now end
runTasks(now + 1)
eq(photos[#photos].raw.masterPhoto.raw.uuid, 'u1', 'the late copy is of the photo that was asked for')

-- 5) a call that comes back too late: timeout first, then the late copy shows up and clears the block
copyMode = 'late'
r = runLoop({ copyOf('u1') })
eq(r[1].ok, false); assert(r[1].error:find('virtual_copy_timeout'), r[1].error)
runTasks(now + 60) -- the late call finishes and makes its copy
copyMode = 'returns'
r = runLoop({ copyOf('u2') })
eq(r[1].ok, true, 'a finished late call no longer blocks: ' .. tostring(r[1].error))

-- 6) an error from Lightroom is reported, not swallowed into a timeout
fakeCatalog.createVirtualCopies = function() error('no photo selected', 0) end
r, t0 = runLoop({ copyOf('u1') })
eq(r[1].ok, false); assert(r[1].error:find('no photo selected'), r[1].error)
assert(r[1].at - t0 <= 2, 'errors answer at once')

print('virtual copy ok')
