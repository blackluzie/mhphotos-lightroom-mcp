-- Catalog audit (read only): pages through every photo of the catalog and reports what the
-- server needs to find photos in the wrong folder and duplicates. Uses the batch metadata
-- calls, because single getRawMetadata calls are far too slow for 250k+ photos.

local LrApplication = import 'LrApplication'
local LrDate = import 'LrDate'
local LrTasks = import 'LrTasks'

local Audit = {}

local MAX_LIMIT = 2000
local CACHE_TTL = 900 -- the photo list of a running audit is reused across pages
local cached

local BASE_RAW = { 'uuid', 'path', 'fileSize', 'fileFormat', 'dimensions', 'dateTimeOriginal',
  'isVirtualCopy', 'rating', 'pickStatus' }
-- Keys that may not exist in every Lightroom version: asked one by one, failures are reported.
local OPTIONAL_RAW = { 'dateTimeOriginalISO8601', 'keywords', 'colorNameForLabel', 'lastEditTime',
  'countVirtualCopies', 'isoSpeedRating', 'aperture' }
local FORMATTED = { 'cameraModel', 'cameraSerialNumber' }

local function catalog() return LrApplication.activeCatalog() end

local function allPhotos(refresh)
  local now = os.time()
  if refresh or not cached or now - cached.at > CACHE_TTL then
    cached = { at = now, photos = catalog():getAllPhotos() }
  end
  return cached.photos
end

local function cachedList(refresh)
  allPhotos(refresh)
  return cached
end

-- Capture time as the camera's wall clock (folders are named after it), not UTC.
local function localTime(iso, raw)
  if type(iso) == 'string' and iso:match('^%d%d%d%d%-%d%d%-%d%dT%d%d:%d%d:%d%d') then
    return iso:sub(1, 19)
  end
  if type(raw) ~= 'number' then return nil end
  local ok, s = LrTasks.pcall(LrDate.timeToUserFormat, raw, '%Y-%m-%dT%H:%M:%S', false)
  if ok and type(s) == 'string' and s:match('^%d%d%d%d%-%d%d%-%d%dT') then return s end
  return nil
end

-- Capture time of one photo as the camera's wall clock (YYYY-MM-DDTHH:MM:SS), '' when unknown.
function Audit.captureTime(photo)
  local ok, iso = LrTasks.pcall(function() return photo:getRawMetadata('dateTimeOriginalISO8601') end)
  return localTime(ok and iso or nil, photo:getRawMetadata('dateTimeOriginal')) or ''
end

local function batch(cat, slice, keys, formatted)
  if formatted then return cat:batchGetFormattedMetadata(slice, keys) end
  return cat:batchGetRawMetadata(slice, keys)
end

local function nonEmpty(v)
  if v == nil or v == '' or v == false then return nil end
  return v
end

-- args: offset (default 0), limit (default 1000, max 2000), refresh (re-read the photo list)
function Audit.page(args)
  local offset = math.max(0, math.floor(tonumber(args.offset) or 0))
  local limit = math.max(1, math.min(math.floor(tonumber(args.limit) or 1000), MAX_LIMIT))
  local photos = allPhotos(args.refresh == true or offset == 0)
  local slice = {}
  for i = offset + 1, math.min(offset + limit, #photos) do slice[#slice + 1] = photos[i] end
  local out = { total = #photos, offset = offset, next_offset = offset + #slice, photos = {}, unsupported = {} }
  if #slice == 0 then return out end

  local cat = catalog()
  local raw = batch(cat, slice, BASE_RAW, false) or {}
  local extra = {}
  for _, key in ipairs(OPTIONAL_RAW) do
    local ok, res = LrTasks.pcall(batch, cat, slice, { key }, false)
    if ok and type(res) == 'table' then extra[key] = res else out.unsupported[#out.unsupported + 1] = key end
  end
  local okF, fmt = LrTasks.pcall(batch, cat, slice, FORMATTED, true)
  if not okF or type(fmt) ~= 'table' then
    fmt = {}
    out.unsupported[#out.unsupported + 1] = 'formatted'
  end

  for _, photo in ipairs(slice) do
    local r = raw[photo] or {}
    local function opt(key)
      local t = extra[key] and extra[key][photo]
      return t and t[key]
    end
    local f = fmt[photo] or {}
    local dims = type(r.dimensions) == 'table' and r.dimensions or {}
    local kws = opt('keywords')
    local edited = opt('lastEditTime')
    out.photos[#out.photos + 1] = {
      id = r.uuid,
      path = r.path,
      size = r.fileSize,
      fmt = nonEmpty(r.fileFormat),
      w = dims.width,
      h = dims.height,
      t = localTime(opt('dateTimeOriginalISO8601'), r.dateTimeOriginal),
      vc = r.isVirtualCopy == true or nil,
      r = nonEmpty(r.rating ~= 0 and r.rating or nil),
      pick = (r.pickStatus == 1 or r.pickStatus == -1) and r.pickStatus or nil,
      kw = type(kws) == 'table' and #kws > 0 and #kws or nil,
      label = nonEmpty(opt('colorNameForLabel') ~= 'grey' and opt('colorNameForLabel') or nil),
      edited = type(edited) == 'number' and edited > 0 or nil,
      copies = nonEmpty((tonumber(opt('countVirtualCopies')) or 0) > 0 and opt('countVirtualCopies') or nil),
      -- exposure data: screenshots and downloaded images have none
      exif = (opt('isoSpeedRating') ~= nil or opt('aperture') ~= nil) or nil,
      cam = nonEmpty(f.cameraModel),
      serial = nonEmpty(f.cameraSerialNumber),
    }
  end
  return out
end

-- ---------------------------------------------------------------- photos by capture date
-- A capture-time index of the whole catalog (built once per cached photo list, in batches), so
-- date queries over 250k+ photos do not walk every photo with single metadata calls.
local CHUNK = 5000

local function dateIndex(refresh)
  local list = cachedList(refresh)
  if list.index then return list.index end
  local cat, index = catalog(), {}
  for start = 1, #list.photos, CHUNK do
    local slice = {}
    for i = start, math.min(start + CHUNK - 1, #list.photos) do slice[#slice + 1] = list.photos[i] end
    local raw = batch(cat, slice, { 'dateTimeOriginal' }, false) or {}
    local okI, iso = LrTasks.pcall(batch, cat, slice, { 'dateTimeOriginalISO8601' }, false)
    if not okI or type(iso) ~= 'table' then iso = {} end
    for _, p in ipairs(slice) do
      local t = localTime(iso[p] and iso[p].dateTimeOriginalISO8601, (raw[p] or {}).dateTimeOriginal)
      if t then index[#index + 1] = { photo = p, t = t } end
    end
  end
  table.sort(index, function(a, b) return a.t < b.t end)
  list.index = index
  return index
end

local function hasInstagramKeyword(kws)
  for _, kw in ipairs(type(kws) == 'table' and kws or {}) do
    local ok, name = LrTasks.pcall(function() return kw:getName() end)
    if ok and type(name) == 'string' and name:lower() == 'instagram' then return true end
  end
  return false
end

-- args: captured_from, captured_to (YYYY-MM-DD, inclusive), offset, limit (max 2000), refresh
function Audit.range(args)
  local from, to = args.captured_from, args.captured_to
  if type(from) ~= 'string' or type(to) ~= 'string' or not from:match('^%d%d%d%d%-%d%d%-%d%d$')
      or not to:match('^%d%d%d%d%-%d%d%-%d%d$') then
    error('captured_from and captured_to must be YYYY-MM-DD', 0)
  end
  local offset = math.max(0, math.floor(tonumber(args.offset) or 0))
  local limit = math.max(1, math.min(math.floor(tonumber(args.limit) or 1000), MAX_LIMIT))
  local hits = {}
  for _, e in ipairs(dateIndex(args.refresh == true)) do
    local day = e.t:sub(1, 10)
    if day > to then break end
    if day >= from then hits[#hits + 1] = e end
  end
  local out = { total = #hits, offset = offset, next_offset = offset, photos = {} }
  local slice, times = {}, {}
  for i = offset + 1, math.min(offset + limit, #hits) do
    slice[#slice + 1] = hits[i].photo
    times[hits[i].photo] = hits[i].t
  end
  out.next_offset = offset + #slice
  if #slice == 0 then return out end
  local cat = catalog()
  local raw = batch(cat, slice, { 'uuid', 'rating', 'pickStatus', 'gps', 'isVirtualCopy' }, false) or {}
  local okK, kws = LrTasks.pcall(batch, cat, slice, { 'keywords' }, false)
  if not okK or type(kws) ~= 'table' then kws = {} end
  local okF, fmt = LrTasks.pcall(batch, cat, slice,
    { 'fileName', 'cameraModel', 'title', 'caption', 'location', 'city', 'stateProvince', 'country' }, true)
  if not okF or type(fmt) ~= 'table' then fmt = {} end
  for _, photo in ipairs(slice) do
    local r, f = raw[photo] or {}, fmt[photo] or {}
    local k = kws[photo] and kws[photo].keywords
    local gps = type(r.gps) == 'table' and r.gps.latitude and { latitude = r.gps.latitude, longitude = r.gps.longitude } or nil
    local place = {}
    for _, key in ipairs({ 'location', 'city', 'stateProvince', 'country' }) do
      if type(f[key]) == 'string' and f[key] ~= '' then place[#place + 1] = f[key] end
    end
    out.photos[#out.photos + 1] = {
      id = r.uuid, file = f.fileName, t = times[photo], cam = nonEmpty(f.cameraModel),
      r = nonEmpty(r.rating ~= 0 and r.rating or nil),
      pick = (r.pickStatus == 1 or r.pickStatus == -1) and r.pickStatus or nil,
      vc = r.isVirtualCopy == true or nil,
      gps = gps,
      place = #place > 0 and table.concat(place, ', ') or nil,
      kw = type(k) == 'table' and #k > 0 and #k or nil,
      insta = hasInstagramKeyword(k) or nil,
      title = nonEmpty(f.title ~= nil and f.title ~= '' or nil),
      caption = nonEmpty(f.caption ~= nil and f.caption ~= '' or nil),
    }
  end
  return out
end

function Audit.clearCache() cached = nil end

return Audit
