-- Tests virtual copy jobs through the REAL Loop.lua and Commands.lua against a cooperative LrTasks fake (the same
-- scheduler as loop_test.lua: tasks are coroutines, startAsyncTask only queues, sleep/yield and LrHttp.post hand over
-- control, time only moves when every task waits).
--
-- The Lightroom behaviour modelled here is what Lightroom Classic did on 06.11.2026 (plug-in 0.1.21 to 0.1.24):
-- * catalog:createVirtualCopies makes the copies of the selected photos but never comes back to its task;
-- * for 300 s after a copy was made every read of THAT copy takes 120 s (older photos answer at once);
-- * while such a call is pending, changing the selection waits until Lightroom is done with it.
-- 0.1.24 counted 40 rounds of sleep(0.5) as "20 s" and read the new copies inside the polling task: 8 photos kept the
-- bridge on "running create_virtual_copies" for more than 13 minutes. Here every command must answer within
-- MAX_ANSWER seconds of being fetched, whatever Lightroom does.
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

local MAX_ANSWER = 10 -- s from fetching a command to posting its result (probe 8 s + slack)

local now = 1000
os.time = function() return math.floor(now) end -- luacheck: ignore

-- ------------------------------------------------------------ cooperative LrTasks (as in loop_test.lua)
local tasks = {}
local current
local nestedTasksStall = false -- a task started from inside another task does not run (what 0.1.22 ran into)

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
local FRESH_SLOW, SLOW_READ = 300, 120
local nextLocalId = 0
local slowReads = 0
local function newPhoto(uuid)
  nextLocalId = nextLocalId + 1
  local p = { localIdentifier = nextLocalId, raw = { uuid = uuid, virtualCopies = {} }, fmt = { fileName = uuid .. '.dng' } }
  local function slow(self)
    if self.made and now - self.made < FRESH_SLOW then slowReads = slowReads + 1; Tasks.sleep(SLOW_READ) end
  end
  function p:getRawMetadata(k) slow(self); return self.raw[k] end
  function p:getFormattedMetadata(k) slow(self); return self.fmt[k] end
  function p:getPropertyForPlugin() return nil end
  return p
end
local photos = {}
for i = 1, 10 do photos[i] = newPhoto('u' .. i) end
local selection = {}
local callPendingSince -- a createVirtualCopies that has not come back
-- 'hangs' (makes the copies, never comes back, like Lightroom Classic), 'returns' (as the SDK says),
-- 'late' (comes back and copies only after 40 s), 'fails'
local copyMode = 'hangs'
local copyCalls = 0

local function photoById(id) for _, p in ipairs(photos) do if p.raw.uuid == id then return p end end end

local fakeCatalog = {
  findPhotoByUuid = function(_, id) return photoById(id) end,
  setSelectedPhotos = function(_, active, others)
    -- Lightroom finishes the pending "Create Virtual Copy" before it changes the selection
    while callPendingSince and now - callPendingSince < FRESH_SLOW do Tasks.sleep(1) end
    selection = { active }
    for _, o in ipairs(others or {}) do selection[#selection + 1] = o end
  end,
  getTargetPhotos = function() return selection end,
  getTargetPhoto = function() return selection[1] end,
  setActiveSources = function() return true end,
  getChildCollections = function() return { { localIdentifier = 7, getName = function() return 'Kreuzfahrt 2026' end } } end,
  getChildCollectionSets = function() return {} end,
  withWriteAccessDo = function(_, _, fn) fn(); return 'executed' end,
  createVirtualCopies = function(_, copyName)
    copyCalls = copyCalls + 1
    if copyMode == 'fails' then error('no photo selected', 0) end
    if copyMode == 'late' then Tasks.sleep(40) end
    local made = {}
    for _, master in ipairs(selection) do
      local copy = newPhoto('copy' .. (#photos + 1) .. '-of-' .. master.raw.uuid)
      copy.made = now
      copy.raw.isVirtualCopy, copy.raw.masterPhoto, copy.raw.copyName = true, master, copyName
      photos[#photos + 1] = copy
      master.raw.virtualCopies[#master.raw.virtualCopies + 1] = copy
      made[#made + 1] = copy
    end
    if copyMode == 'hangs' then callPendingSince = now; Tasks.sleep(math.huge) end -- never comes back
    return made
  end,
}
local catalogProxy = setmetatable({ kAllPhotos = 'kAllPhotos' }, { __index = function(_, k)
  local f = fakeCatalog[k]
  if f == nil then return nil end
  return function(self, ...) return f(self, ...) end end })

-- ------------------------------------------------------------ fake HTTP server + imports
local posts, script = {}, {}
local State
_PLUGIN = { id = 'plugin' }

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
local Commands = require 'Commands'

local seq = 0
local function command(name, args)
  return function()
    seq = seq + 1
    posts[#posts + 1] = { path = 'fetched', name = name, at = now }
    return Json.encode({ command = { id = name .. '-' .. seq, name = name, args = args or {} } }), { status = 200 }
  end
end
-- an idle long poll: the server has nothing for s seconds
local function idle(s)
  return function() Tasks.sleep(s); return '{"command":null}', { status = 200 } end
end

-- runs the loop over the steps; returns the results in order, each with .at, .took (s since fetched) and .name
local function runLoop(steps)
  posts, script, State.stop = {}, steps, false
  Tasks.startAsyncTask(function()
    local ok, err = Tasks.pcall(Loop.run)
    if not ok then error('loop crashed: ' .. tostring(err), 0) end
  end, 'MHPhotos: bridge loop')
  runTasks(now + 7200)
  local out, fetched = {}, nil
  for _, p in ipairs(posts) do
    if p.path == 'fetched' then fetched = p end
    if p.path == '/v1/result' then
      local r = Json.decode(p.body)
      r.at, r.took, r.name = p.at, p.at - fetched.at, fetched.name
      assert(r.took <= MAX_ANSWER, r.name .. ' blocked the polling task for ' .. r.took .. ' s')
      out[#out + 1] = r
    end
  end
  eq(State.status, 'stopped', 'the loop ended normally (script empty), it was not stuck')
  return out
end

local function ids(n)
  local t = {}
  for i = 1, n do t[#t + 1] = 'u' .. i end
  return t
end
local function copyJob(list)
  return command('create_virtual_copies', { photo_ids = list, copy_name = 'Insta 9:16' })
end
-- the newest job (the server reads its id from the answer of create_virtual_copies)
local jobId
local function newestJob()
  local best, bestN = nil, -1
  for id in pairs(Commands._vc.jobs) do
    local n = tonumber(id:match('^vc(%d+)-'))
    if n > bestN then best, bestN = id, n end
  end
  return best
end
local function pollJob() return command('virtual_copy_job', { job_id = jobId })() end
-- start a job, then poll its status n times, every `every` seconds
local function startAndPoll(list, n, every, extra)
  local steps = { copyJob(list), function() jobId = newestJob(); return '{"command":null}', { status = 200 } end }
  for i = 1, n do
    steps[#steps + 1] = idle(every)
    steps[#steps + 1] = pollJob
    if extra and i == 3 then steps[#steps + 1] = extra end
  end
  return steps
end
local function last(results, name)
  for i = #results, 1, -1 do if results[i].name == name then return results[i] end end
end

-- 1) THE 8-PHOTO CASE: one call, Lightroom hangs on it, new copies read slowly, the selection waits for Lightroom.
-- The start answers at once, every status poll answers at once, other commands too, and the job ends with 8 copies.
local r = runLoop(startAndPoll(ids(8), 45, 15, command('list_collections')))
eq(r[1].name, 'create_virtual_copies'); eq(r[1].ok, true, tostring(r[1].error))
eq(r[1].data.state, 'running'); eq(r[1].data.total, 8)
assert(r[1].took <= 1, 'the start answers at once, took ' .. r[1].took)
eq(copyCalls, 1, 'one createVirtualCopies for the whole batch')
local lc = last(r, 'list_collections')
eq(lc.ok, true); eq(lc.data.collections[1].name, 'Kreuzfahrt 2026')
local final = last(r, 'virtual_copy_job').data
eq(final.state, 'done', 'job ends: ' .. tostring(final.error))
eq(#final.copies, 8)
for i, c in ipairs(final.copies) do
  eq(c.source_id, 'u' .. i)
  eq(c.id, photos[i].raw.virtualCopies[1].raw.uuid)
  eq(photos[i].raw.virtualCopies[1].raw.copyName, 'Insta 9:16')
end
assert(slowReads > 0, 'the slow reads happened (in the worker)')
local sawRunning = false
for _, x in ipairs(r) do
  if x.name == 'virtual_copy_job' then
    assert(x.took <= 1, 'status polls never touch the catalog, took ' .. x.took)
    if x.data.state == 'running' then sawRunning = true end
  end
end
assert(sawRunning, 'the status showed the job running before it was done')
local firstCopy = final.copies[1].id

-- 2) a command on a new copy while Lightroom still finishes it: photo_busy within the probe time, nothing hangs;
-- once Lightroom answers (the probe got through) the same command works
local fresh = photoById(firstCopy)
fresh.made = now -- as if it had just been made (Lightroom slow on it, the plugin still counts it as new)
Commands._vc.fresh[firstCopy] = now
r = runLoop({ command('get_photo', { photo_id = firstCopy }), command('list_collections'),
  command('apply_develop_settings', { photo_ids = { firstCopy }, settings = { CropTop = 0 } }) })
eq(r[1].ok, false); assert(r[1].error:find('photo_busy'), r[1].error)
assert(r[1].took >= 7 and r[1].took <= 9, 'bounded probe, took ' .. r[1].took)
eq(r[2].ok, true, 'other commands answer meanwhile')
eq(r[3].ok, false); assert(r[3].error:find('photo_busy'), 'writes on a busy copy are not started either: ' .. r[3].error)
runTasks(now + FRESH_SLOW) -- the orphaned probe finishes
fresh.made = nil
r = runLoop({ command('get_photo', { photo_id = firstCopy }) })
eq(r[1].ok, true, tostring(r[1].error)); eq(r[1].data.virtual_copy_of, 'u1')
eq(Commands._vc.fresh[firstCopy], nil, 'a copy that answered is no longer probed')

-- 3) a call that never came back: while job 1 runs, and for a while after its copies showed up, a new copy job is
-- refused (copy_pending: Lightroom might still copy whatever is selected); later it is allowed again
local callsBefore = copyCalls
r = runLoop(startAndPoll({ 'u9' }, 12, 15, copyJob({ 'u10' })))
local refused = last(r, 'create_virtual_copies')
eq(refused.ok, false); assert(refused.error:find('copy_pending'), refused.error)
assert(refused.error:find("Cmd+'", 1, true), 'error names the manual way: ' .. refused.error)
eq(last(r, 'virtual_copy_job').data.state, 'done')
eq(copyCalls, callsBefore + 1, 'only the first job called Lightroom')
r = runLoop({ copyJob({ 'u10' }) })
eq(r[1].ok, false); assert(r[1].error:find('still finishing'), 'refused after the copies showed up: ' .. r[1].error)
eq(copyCalls, callsBefore + 1, 'no createVirtualCopies ran')
now = now + 400
copyMode = 'returns'
r = runLoop(startAndPoll({ 'u10' }, 12, 15)) -- the new copy's id is read slowly (in the worker)
eq(r[1].ok, true, tostring(r[1].error))
local j3 = last(r, 'virtual_copy_job').data
eq(j3.state, 'done', tostring(j3.error)); eq(j3.copies[1].source_id, 'u10')
eq(j3.copies[1].id, photos[10].raw.virtualCopies[1].raw.uuid)

-- 4) Lightroom reports an error: the job fails with that message and the manual way, quickly
copyMode = 'fails'
r = runLoop(startAndPoll({ 'u10' }, 2, 2))
local j4 = last(r, 'virtual_copy_job').data
eq(j4.state, 'failed'); assert(j4.error:find('no photo selected'), j4.error)
assert(j4.error:find('virtual_copies=false', 1, true), j4.error)
eq(j4.missing[1], 'u10')
assert(j4.elapsed <= 5, 'errors end the job at once')

-- 5) a call that comes back too late: the job reports timeout first; the late copy is still reported afterwards
copyMode = 'late'
r = runLoop(startAndPoll({ 'u10' }, 6, 5))
local j5 = last(r, 'virtual_copy_job').data
eq(j5.state, 'timeout'); assert(j5.error:find('within'), j5.error)
eq(#j5.copies, 0)
runTasks(now + 60)
r = runLoop({ pollJob })
eq(#r[1].data.copies, 1, 'the late copy shows up in the status'); eq(r[1].data.copies[1].source_id, 'u10')
runTasks(now + 400)

-- 6) the worker never runs (as with 0.1.22's nested tasks): the status keeps answering "running"; after COPY_WAIT a
-- new job replaces it, and if Lightroom runs the old worker later it does nothing
copyMode = 'returns'
nestedTasksStall = true
local photosBefore = #photos
r = runLoop(startAndPoll({ 'u3' }, 2, 5))
eq(last(r, 'virtual_copy_job').data.state, 'running'); eq(last(r, 'virtual_copy_job').data.phase, 'queued')
local stalled = jobId
r = runLoop({ copyJob({ 'u3' }) })
eq(r[1].ok, false); assert(r[1].error:find('copy_pending'), 'a queued job blocks for COPY_WAIT: ' .. r[1].error)
now = now + 30
nestedTasksStall = false
r = runLoop({ copyJob({ 'u4' }) })
eq(r[1].ok, true, 'after COPY_WAIT the stalled job is given up: ' .. tostring(r[1].error))
eq(Commands._vc.jobs[stalled].state, 'failed')
for _, t in ipairs(tasks) do if t.wake == math.huge then t.wake = now end end
runTasks(now + 120)
eq(#photos, photosBefore + 1, 'only the new job made a copy (of u4), the stalled one stopped')
eq(photos[#photos].raw.masterPhoto.raw.uuid, 'u4')

print('virtual copy ok')
