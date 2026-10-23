-- Optional auto-export: every few minutes, export the best new photos of ONE collection as JPEG
-- into a folder (by default the hot folder that the inbox watcher uploads from).
--
-- Off by default. Configured only through ~/.mhphotos-lightroom-mcp/autoexport.json, never over
-- the MCP bridge, so nothing remote can switch it on. Starts in dry-run mode (it only reports
-- what it would export). Each photo is exported once (remembered in autoexport-done.json) and at
-- most max_per_run photos per run. It never writes to the catalog and never publishes anything:
-- the inbox only holds photos with status "wartet" until you approve them in MHPhotos.

local Json = require 'Json'

local AutoExport = {}

local DEFAULT_DEST = '~/Pictures/MHPhotos-Inbox'

local function clamp(v, lo, hi, default)
  v = tonumber(v)
  if not v then return default end
  return math.max(lo, math.min(hi, math.floor(v)))
end

-- Turns the decoded JSON into a safe config. Returns nil when auto-export is not enabled.
-- Returns nil, reason when it is enabled but unusable (reported in the status file).
function AutoExport.normalize(raw, home)
  if type(raw) ~= 'table' or raw.enabled ~= true then return nil end
  local collection = type(raw.collection) == 'string' and raw.collection or ''
  if collection == '' then return nil, 'enabled, but "collection" is missing' end
  local dest = type(raw.dest_dir) == 'string' and raw.dest_dir ~= '' and raw.dest_dir or DEFAULT_DEST
  if dest:sub(1, 2) == '~/' then dest = home .. dest:sub(2) end
  if dest:sub(1, 1) ~= '/' then return nil, 'dest_dir must be an absolute path' end
  return {
    collection = collection,
    min_rating = clamp(raw.min_rating, 1, 5, 4),
    max_per_run = clamp(raw.max_per_run, 1, 20, 5),
    interval_minutes = clamp(raw.interval_minutes, 5, 1440, 15),
    dry_run = raw.dry_run ~= false, -- anything but an explicit false keeps the dry run
    dest_dir = dest,
  }
end

local function findCollection(deps, wanted)
  local want = wanted:lower()
  for _, c in ipairs(deps.list_collections({}).collections) do
    local name = c.name:lower()
    if name == want or name:match('([^/]*)$') == want then return c end
  end
  return nil
end

-- One run. deps: list_collections(args), list_photos(args), export_photos(args).
-- done: set {uuid = true} of photos exported before (updated in place).
-- Returns a table with status = 'idle' | 'dry-run' | 'exported' | 'error'.
function AutoExport.run_once(cfg, done, deps)
  local coll = findCollection(deps, cfg.collection)
  if not coll then return { status = 'error', error = 'collection not found: ' .. cfg.collection } end

  local fresh, offset = {}, 0
  while #fresh < cfg.max_per_run and offset < 2000 do
    local page = deps.list_photos({
      collection_id = coll.id, limit = 100, offset = offset, filters = { min_rating = cfg.min_rating },
    })
    for _, p in ipairs(page.photos) do
      if not done[p.id] and #fresh < cfg.max_per_run then fresh[#fresh + 1] = p end
    end
    offset = offset + 100
    if offset >= page.total then break end
  end
  if #fresh == 0 then return { status = 'idle', exported = 0 } end

  local ids, names = {}, {}
  for i, p in ipairs(fresh) do ids[i] = p.id; names[i] = p.filename end
  if cfg.dry_run then return { status = 'dry-run', would_export = names } end

  deps.export_photos({ photo_ids = ids, dest_dir = cfg.dest_dir })
  for _, id in ipairs(ids) do done[id] = true end
  return { status = 'exported', exported = #ids, names = names }
end

-- ---------------------------------------------------------------- setup dialog helpers
-- Values shown in the setup dialog; existing = decoded autoexport.json or nil. The first setup
-- starts switched off and in dry-run mode.
function AutoExport.dialog_values(existing)
  local e = type(existing) == 'table' and existing or {}
  return {
    enabled = e.enabled == true,
    collection = type(e.collection) == 'string' and e.collection or '',
    min_rating = clamp(e.min_rating, 1, 5, 4),
    max_per_run = clamp(e.max_per_run, 1, 20, 5),
    interval_minutes = clamp(e.interval_minutes, 5, 1440, 15),
    dry_run = e.dry_run ~= false,
  }
end

-- What the dialog saves. Keeps a dest_dir that was set by hand in the file.
function AutoExport.build_config(values, existing)
  local v = AutoExport.dialog_values(values)
  local cfg = {
    enabled = v.enabled, collection = v.collection, min_rating = v.min_rating,
    max_per_run = v.max_per_run, interval_minutes = v.interval_minutes, dry_run = v.dry_run,
  }
  if type(existing) == 'table' and type(existing.dest_dir) == 'string' and existing.dest_dir ~= '' then
    cfg.dest_dir = existing.dest_dir
  end
  return cfg
end

-- ---------------------------------------------------------------- persistence and loop
function AutoExport.encode_done(done)
  local ids = {}
  for id in pairs(done) do ids[#ids + 1] = id end
  table.sort(ids)
  return Json.encode({ ids = ids })
end

function AutoExport.decode_done(raw)
  local done = {}
  if type(raw) ~= 'string' or raw == '' then return done end
  local ok, data = pcall(Json.decode, raw)
  if ok and type(data) == 'table' and type(data.ids) == 'table' then
    for _, id in ipairs(data.ids) do done[id] = true end
  end
  return done
end

function AutoExport.describe(result)
  if result.status == 'error' then return 'error: ' .. tostring(result.error) end
  if result.status == 'dry-run' then
    return 'dry run: would export ' .. #result.would_export .. ' photo(s): ' .. table.concat(result.would_export, ', ')
  end
  if result.status == 'exported' then return 'exported ' .. result.exported .. ' photo(s)' end
  return 'idle: nothing new'
end

-- Runs inside a Lightroom async task. Re-reads the config on every round, so switching it on or
-- off needs no reload.
function AutoExport.loop()
  local LrTasks = import 'LrTasks'
  local LrPathUtils = import 'LrPathUtils'
  local LrFileUtils = import 'LrFileUtils'
  local State = require 'State'
  local Commands = require 'Commands'

  local home = LrPathUtils.getStandardFilePath('home')
  local dir = LrPathUtils.child(home, '.mhphotos-lightroom-mcp')
  local configPath = LrPathUtils.child(dir, 'autoexport.json')
  local donePath = LrPathUtils.child(dir, 'autoexport-done.json')
  local statusPath = LrPathUtils.child(dir, 'autoexport-status.txt')

  local function status(text)
    pcall(function()
      local f = io.open(statusPath, 'w')
      if f then f:write(os.date('%Y-%m-%d %H:%M:%S ') .. text .. '\n'); f:close() end
    end)
  end

  local deps = {
    list_collections = Commands.list_collections,
    list_photos = Commands.list_photos,
    export_photos = Commands.export_photos,
  }
  local done = AutoExport.decode_done(LrFileUtils.exists(donePath) and LrFileUtils.readFile(donePath) or nil)

  local function nap(seconds)
    for _ = 1, math.max(1, math.floor(seconds / 5)) do
      if State.stop then return end
      LrTasks.sleep(5)
    end
  end

  while not State.stop do
    local cfg, why
    if LrFileUtils.exists(configPath) then
      local ok, raw = pcall(Json.decode, LrFileUtils.readFile(configPath) or '')
      if ok then cfg, why = AutoExport.normalize(raw, home) else why = 'autoexport.json is not valid JSON' end
    end
    if not cfg then
      status(why and ('off: ' .. why) or 'off (no autoexport.json or enabled is not true)')
      nap(30)
    else
      local ok, result = LrTasks.pcall(AutoExport.run_once, cfg, done, deps)
      if not ok then
        result = { status = 'error', error = type(result) == 'table' and tostring(result.message) or tostring(result) }
      end
      if result.status == 'exported' then
        pcall(function()
          local f = io.open(donePath, 'w')
          if f then f:write(AutoExport.encode_done(done)); f:close() end
        end)
      end
      status(AutoExport.describe(result) .. ' | next run in ' .. cfg.interval_minutes .. ' min')
      nap(cfg.interval_minutes * 60)
    end
  end
end

return AutoExport
