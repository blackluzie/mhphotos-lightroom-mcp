-- Lightroom-side command implementations. Every function receives the decoded
-- args table and returns a plain table (JSON-encodable) or raises error().
-- Errors with {code='not_found'} are reported to the MCP side as not_found.

local LrApplication = import 'LrApplication'
local LrTasks = import 'LrTasks'
-- LrTasks.pcall, not pcall: Lightroom's plain pcall cannot yield, and catalog calls yield
-- ("Yielding is not allowed within a C or metamethod call").
local LrStringUtils = import 'LrStringUtils'
local LrDate = import 'LrDate'
local LrApplicationView = import 'LrApplicationView'
local Audit = require 'Audit'

local Commands = {}

local MAX_PAGE = 100

local function notFound(msg) error({ code = 'not_found', message = msg }, 0) end

local function catalog() return LrApplication.activeCatalog() end

-- ---------------------------------------------------------------- helpers
local function pickStatusToFlag(p)
  if p == 1 then return 'picked' end
  if p == -1 then return 'rejected' end
  return 'none'
end

-- Capture time as the camera's wall clock (the old UTC formatting moved photos taken shortly
-- after midnight to the previous day in date filters).
local function captureTime(photo) return Audit.captureTime(photo) end

local function photoSummary(photo)
  return {
    id = photo:getRawMetadata('uuid'),
    filename = photo:getFormattedMetadata('fileName') or '',
    rating = photo:getRawMetadata('rating') or 0,
  }
end

local function gpsOf(photo)
  local g = photo:getRawMetadata('gps')
  if type(g) == 'table' and g.latitude and g.longitude then return g.latitude, g.longitude end
  return nil
end

local function locationString(photo)
  local parts = {}
  for _, key in ipairs({ 'location', 'city', 'stateProvince', 'country' }) do
    local v = photo:getFormattedMetadata(key)
    if type(v) == 'string' and v ~= '' then parts[#parts + 1] = v end
  end
  return table.concat(parts, ', ')
end

local function distanceKm(lat1, lon1, lat2, lon2)
  local rad = math.pi / 180
  local dlat, dlon = (lat2 - lat1) * rad, (lon2 - lon1) * rad
  local a = math.sin(dlat / 2) ^ 2 + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin(dlon / 2) ^ 2
  return 6371 * 2 * math.asin(math.min(1, math.sqrt(a)))
end

local function photoMetadata(photo)
  local s = photoSummary(photo)
  local keywords = {}
  for _, kw in ipairs(photo:getRawMetadata('keywords') or {}) do
    keywords[#keywords + 1] = kw:getName()
  end
  s.flag = pickStatusToFlag(photo:getRawMetadata('pickStatus'))
  s.keywords = keywords
  s.capture_time = captureTime(photo)
  s.camera = photo:getFormattedMetadata('cameraModel') or ''
  s.iso = photo:getRawMetadata('isoSpeedRating')
  s.aperture = photo:getRawMetadata('aperture')
  s.shutter_speed = photo:getFormattedMetadata('shutterSpeed')
  s.focal_length_mm = photo:getRawMetadata('focalLength')
  s.lens = photo:getFormattedMetadata('lens')
  local title, caption = photo:getFormattedMetadata('title'), photo:getFormattedMetadata('caption')
  if type(title) == 'string' and title ~= '' then s.title = title end
  if type(caption) == 'string' and caption ~= '' then s.caption = caption end
  local label = photo:getRawMetadata('colorNameForLabel')
  if type(label) == 'string' and label ~= '' then
    -- Lightroom reports "no colour" as grey; the tools call it none (see set_color_label)
    if label == 'grey' or label == 'gray' then label = 'none' end
    s.color_label = label
  end
  if photo:getRawMetadata('isVirtualCopy') then
    local master = photo:getRawMetadata('masterPhoto')
    if master then s.virtual_copy_of = master:getRawMetadata('uuid') end
  end
  local okS, status = LrTasks.pcall(function() return photo:getPropertyForPlugin(_PLUGIN, 'status') end)
  if okS and type(status) == 'string' and status ~= '' then s.mhphotos_status = status end
  local okN, own = LrTasks.pcall(function() return photo:getPropertyForPlugin(_PLUGIN, 'notiz') end)
  if okN and type(own) == 'string' and own ~= '' then s.mhphotos_note = own end
  local place = locationString(photo)
  if place ~= '' then s.location = place end
  local lat, lon = gpsOf(photo)
  if lat then s.gps = { latitude = lat, longitude = lon } end
  return s
end

local function findPhoto(id)
  local photo = catalog():findPhotoByUuid(id)
  if not photo then notFound('Photo not found: ' .. tostring(id)) end
  return photo
end

local function findAll(ids)
  -- Resolve every id first so a bad id aborts before anything is changed.
  local photos = {}
  for i, id in ipairs(ids or {}) do photos[i] = findPhoto(id) end
  if #photos == 0 then error('photo_ids must not be empty', 0) end
  if #photos > MAX_PAGE then error('too many photos in one call', 0) end
  return photos
end

-- Filtered id lists are cached briefly so paging through results does not rescan the catalog.
-- Any write command clears the cache.
local filterCache = {}
local CACHE_TTL = 60
local function invalidateCache() filterCache = {} end

local function writeAccess(name, fn)
  invalidateCache()
  -- asynchronous=false: wait here (up to 15 s) instead of queueing the action for later
  local result = catalog():withWriteAccessDo(name, fn, { timeout = 15, asynchronous = false })
  if result ~= 'executed' then
    error('write_blocked: Lightroom did not grant catalog write access (' .. tostring(result) .. ')', 0)
  end
end

-- ---------------------------------------------------------------- reads
local function walkCollections(parent, prefix, out, withCounts)
  for _, c in ipairs(parent:getChildCollections() or {}) do
    local entry = { id = tostring(c.localIdentifier), name = prefix .. c:getName() }
    -- counting walks every photo of the collection: only on request (slow for smart collections)
    if withCounts then entry.photo_count = #(c:getPhotos() or {}) end
    out[#out + 1] = entry
  end
  for _, set in ipairs(parent:getChildCollectionSets() or {}) do
    walkCollections(set, prefix .. set:getName() .. '/', out, withCounts)
  end
end

function Commands.list_collections(args)
  local out = {}
  walkCollections(catalog(), '', out, args and args.include_counts == true)
  return { collections = out }
end

-- Filter criteria (all optional, all must match): min_rating, captured_from, captured_to
-- (YYYY-MM-DD, inclusive), keyword (exact, case-insensitive), flag.
local function matchesFilters(photo, f)
  if f.min_rating and (photo:getRawMetadata('rating') or 0) < f.min_rating then return false end
  if f.captured_from or f.captured_to then
    local day = captureTime(photo):sub(1, 10)
    if day == '' then return false end
    if f.captured_from and day < f.captured_from then return false end
    if f.captured_to and day > f.captured_to then return false end
  end
  if f.flag and pickStatusToFlag(photo:getRawMetadata('pickStatus')) ~= f.flag then return false end
  if f.location and not locationString(photo):lower():find(f.location:lower(), 1, true) then
    return false
  end
  if f.near_lat then
    local lat, lon = gpsOf(photo)
    if not lat or distanceKm(f.near_lat, f.near_lon, lat, lon) > f.radius_km then return false end
  end
  if f.keyword then
    local want, found = f.keyword:lower(), false
    for _, kw in ipairs(photo:getRawMetadata('keywords') or {}) do
      if kw:getName():lower() == want then found = true; break end
    end
    if not found then return false end
  end
  return true
end

-- Lightroom's own index (catalog:findPhotos) answers date/rating/keyword/flag filters without
-- reading all photos. The result is still checked with matchesFilters, and any failure falls back to
-- the full scan, so a wrong guess about the search syntax can only cost time, never correctness.
local function dayBefore(day) -- 'YYYY-MM-DD' -> the day before (the index may count by UTC)
  local y, m, d = day:match('^(%d%d%d%d)-(%d%d)-(%d%d)$')
  if not y then return day end
  local t = os.time({ year = tonumber(y), month = tonumber(m), day = tonumber(d) - 1, hour = 12 })
  return os.date('%Y-%m-%d', t)
end

local function dayAfter(day)
  local y, m, d = day:match('^(%d%d%d%d)-(%d%d)-(%d%d)$')
  if not y then return day end
  local t = os.time({ year = tonumber(y), month = tonumber(m), day = tonumber(d) + 1, hour = 12 })
  return os.date('%Y-%m-%d', t)
end

-- searchDesc for the indexable part of the filters, or nil if nothing is indexable.
-- widen: dates one day wider on both sides (exact matching is done afterwards).
local function searchDescFor(f, widen)
  local crit = {}
  if f.captured_from or f.captured_to then
    local from = f.captured_from or '1900-01-01'
    local to = f.captured_to or '2999-12-31'
    if widen then from, to = dayBefore(from), dayAfter(to) end
    crit[#crit + 1] = { criteria = 'captureTime', operation = 'in', value = from, value2 = to }
  end
  if f.min_rating and f.min_rating > 0 then
    crit[#crit + 1] = { criteria = 'rating', operation = '>=', value = f.min_rating }
  end
  if f.keyword then crit[#crit + 1] = { criteria = 'keywords', operation = 'words', value = f.keyword } end
  if f.flag == 'picked' then crit[#crit + 1] = { criteria = 'pick', operation = '==', value = 1 } end
  if f.flag == 'rejected' then crit[#crit + 1] = { criteria = 'pick', operation = '==', value = -1 } end
  if #crit == 0 then return nil end
  crit.combine = 'intersect'
  return crit
end

local function fastSearch(filters)
  local desc = searchDescFor(filters, true)
  if not desc then return nil end
  local cat = catalog()
  if not cat.findPhotos then return nil end
  local ok, found = LrTasks.pcall(function() return cat:findPhotos({ searchDesc = desc }) end)
  if not ok or type(found) ~= 'table' then return nil end
  return found
end

local function cacheKey(args)
  local parts = { tostring(args.collection_id) }
  local names = {}
  for k in pairs(args.filters) do names[#names + 1] = k end
  table.sort(names)
  for _, k in ipairs(names) do parts[#parts + 1] = k .. '=' .. tostring(args.filters[k]) end
  return table.concat(parts, '|')
end

function Commands.list_photos(args)
  local limit = math.max(1, math.min(tonumber(args.limit) or 25, MAX_PAGE))
  local offset = math.max(0, tonumber(args.offset) or 0)
  local filters = args.filters
  if filters and next(filters) == nil then filters = nil end
  local source
  if args.collection_id then
    local coll = catalog():getCollectionByLocalIdentifier(tonumber(args.collection_id))
    if not coll then notFound('Collection not found: ' .. tostring(args.collection_id)) end
    if not coll.getPhotos then error('this id is a collection set (it holds collections, not photos)', 0) end
    source = coll:getPhotos()
  else
    source = filters and fastSearch(filters) or catalog():getAllPhotos()
  end
  if filters then
    -- Filtering walks every photo once (fine for thousands, slow for 100k+); cached for paging.
    local key = cacheKey({ collection_id = args.collection_id, filters = filters })
    local hit = filterCache[key]
    if hit and os.time() - hit.at <= CACHE_TTL then
      source = hit.photos
    else
      local kept = {}
      for _, p in ipairs(source) do
        if matchesFilters(p, filters) then kept[#kept + 1] = p end
      end
      filterCache[key] = { at = os.time(), photos = kept }
      source = kept
    end
  end
  local photos = {}
  for i = offset + 1, math.min(offset + limit, #source) do
    photos[#photos + 1] = photoSummary(source[i])
  end
  return { total = #source, photos = photos }
end

function Commands.get_photo(args)
  return photoMetadata(findPhoto(args.photo_id))
end

-- Since 0.1.28: metadata of many photos in ONE bridge round (the server's edit preview and contact sheet asked
-- photo by photo, 100 round trips for a 100-photo batch, which ran into the client's time limit).
-- Unknown ids come back as { id, error = 'not_found' } instead of failing the whole call.
function Commands.get_photos(args)
  local ids = args.photo_ids or {}
  if #ids == 0 or #ids > MAX_PAGE then error('1-' .. MAX_PAGE .. ' photo_ids required', 0) end
  local cat, out = catalog(), {}
  for i, id in ipairs(ids) do
    local photo = cat:findPhotoByUuid(id)
    out[i] = photo and photoMetadata(photo) or { id = id, error = 'not_found' }
  end
  return { photos = out }
end

-- ---------------------------------------------------------------- writes
-- The MCP server enforces the permission gate; these still validate inputs.
function Commands.set_rating(args)
  local rating = tonumber(args.rating)
  if not rating or rating < 0 or rating > 5 or rating ~= math.floor(rating) then
    error('rating must be an integer 0-5', 0)
  end
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: set rating', function()
    for _, p in ipairs(photos) do
      if rating == 0 then p:setRawMetadata('rating', nil) else p:setRawMetadata('rating', rating) end
    end
  end)
  return { updated = #photos }
end

function Commands.set_flag(args)
  local map = { none = 0, picked = 1, rejected = -1 }
  local value = map[args.flag]
  if value == nil then error('flag must be none, picked or rejected', 0) end
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: set flag', function()
    for _, p in ipairs(photos) do p:setRawMetadata('pickStatus', value) end
  end)
  return { updated = #photos }
end

-- "Orte > Spanien > Barcelona" -> the keyword Barcelona below Spanien below Orte (levels are created).
local function keywordFromPath(cat, name, synonyms, includeOnExport)
  local parent, kw = nil, nil
  local parts = {}
  for part in string.gmatch(name, '[^>]+') do
    part = part:match('^%s*(.-)%s*$')
    if part ~= '' then parts[#parts + 1] = part end
  end
  if #parts == 0 then error('empty keyword', 0) end
  for i, part in ipairs(parts) do
    local last = i == #parts
    kw = cat:createKeyword(part, last and synonyms or {}, last and includeOnExport or true, parent, true)
    if not kw then error('Lightroom did not create the keyword ' .. part, 0) end
    parent = kw
  end
  return kw
end

function Commands.add_keywords(args)
  local names = args.keywords or {}
  if #names == 0 or #names > 20 then error('1-20 keywords required', 0) end
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: add keywords', function()
    local cat = catalog()
    for _, name in ipairs(names) do
      local kw = string.find(name, '>', 1, true) and keywordFromPath(cat, name, {}, true)
        or cat:createKeyword(name, {}, true, nil, true)
      for _, p in ipairs(photos) do p:addKeyword(kw) end
    end
  end)
  return { updated = #photos }
end

-- Creates a keyword (or a path like "Orte > Spanien > Barcelona") with synonyms, without tagging any photo.
function Commands.create_keyword(args)
  local name = args.name
  if type(name) ~= 'string' or name == '' or #name > 300 then error('keyword name or path required', 0) end
  local synonyms = args.synonyms or {}
  if #synonyms > 20 then error('at most 20 synonyms', 0) end
  local include = args.include_on_export ~= false
  local kw
  writeAccess('MHPhotos: create keyword', function()
    kw = keywordFromPath(catalog(), name, synonyms, include)
  end)
  return { name = kw:getName(), path = name, synonyms = synonyms }
end

function Commands.export_photos(args)
  local LrExportSession = import 'LrExportSession'
  local dest = args.dest_dir
  if type(dest) ~= 'string' or dest == '' then error('dest_dir required', 0) end
  local photos = findAll(args.photo_ids)
  local names = {}
  for i, p in ipairs(photos) do
    names[i] = (p:getFormattedMetadata('fileName') or 'photo'):gsub('%.[^.]*$', '') .. '.jpg'
  end
  local settings = {
    LR_format = 'JPEG',
    LR_jpeg_quality = 0.9,
    LR_export_destinationType = 'specificFolder',
    LR_export_destinationPathPrefix = dest,
    LR_export_useSubfolder = false,
    LR_collisionHandling = 'rename',
    LR_size_doConstrain = false,
    LR_reimportExportedPhoto = false,
    LR_renamingTokensOn = false,
  }
  -- optional: size, quality, sharpening (the MCP server turns presets like "instagram" into these)
  local longEdge = tonumber(args.long_edge)
  if longEdge then
    if longEdge < 200 or longEdge > 10000 or longEdge ~= math.floor(longEdge) then
      error('long_edge must be a whole number of pixels between 200 and 10000', 0)
    end
    settings.LR_size_doConstrain = true
    settings.LR_size_resizeType = 'longEdge'
    settings.LR_size_maxWidth = longEdge
    settings.LR_size_maxHeight = longEdge
    settings.LR_size_units = 'pixels'
    settings.LR_size_doNotEnlarge = true
  end
  local quality = tonumber(args.quality)
  if quality then
    if quality < 1 or quality > 100 then error('quality must be between 1 and 100', 0) end
    settings.LR_jpeg_quality = quality / 100
  end
  if args.sharpen == 'screen' then
    settings.LR_outputSharpeningOn = true
    settings.LR_outputSharpeningMedia = 'screen'
    settings.LR_outputSharpeningLevel = 2
  elseif args.sharpen ~= nil and args.sharpen ~= 'off' then
    error('sharpen must be screen or off', 0)
  end
  settings.LR_export_colorSpace = 'sRGB'
  local session = LrExportSession({ photosToExport = photos, exportSettings = settings })
  session:doExportOnCurrentTask()
  -- Names are the expected output names; Lightroom renames on collision.
  return { exported = names, destination = dest, long_edge = longEdge, quality = quality, sharpen = args.sharpen }
end

-- Since 0.1.28: thumbnails of up to 30 photos requested TOGETHER and awaited against one wall-clock deadline
-- (get_preview waits up to 30 s per photo, one after the other: a contact sheet of 20 uncached photos took
-- minutes and the bridge looked dead meanwhile). Photos Lightroom does not render in time come back with an
-- error, the rest is delivered; the call never keeps the polling task longer than `wait` seconds.
local PREVIEWS_MAX, PREVIEWS_WAIT = 30, 25
function Commands.get_previews(args)
  local ids = args.photo_ids or {}
  if #ids == 0 or #ids > PREVIEWS_MAX then error('1-' .. PREVIEWS_MAX .. ' photo_ids required', 0) end
  local size = math.max(256, math.min(tonumber(args.size) or 768, 1280))
  local wait = math.max(5, math.min(tonumber(args.wait) or PREVIEWS_WAIT, 50))
  local cat, slots, requests = catalog(), {}, {}
  for i, id in ipairs(ids) do
    local slot = { id = id }
    slots[i] = slot
    local photo = cat:findPhotoByUuid(id)
    if not photo then
      slot.error, slot.done = 'not_found', true
    else
      -- keep every request referenced until we are done, or Lightroom may drop the callback
      local ok, req = LrTasks.pcall(function()
        return photo:requestJpegThumbnail(size, size, function(d, e) slot.data, slot.err, slot.done = d, e, true end)
      end)
      if ok then requests[i] = req else slot.error, slot.done = 'preview failed: ' .. tostring(req), true end
    end
  end
  local deadline = LrDate.currentTime() + wait
  local function pending()
    for _, slot in ipairs(slots) do if not slot.done then return true end end
    return false
  end
  while pending() and LrDate.currentTime() < deadline do LrTasks.sleep(0.1) end
  local out = {}
  for i, slot in ipairs(slots) do
    if slot.data then
      out[i] = { id = slot.id, image_base64 = LrStringUtils.encodeBase64(slot.data) }
    else
      out[i] = { id = slot.id, error = slot.error or ('preview failed: ' .. tostring(slot.err or 'timeout')) }
    end
  end
  requests = nil -- luacheck: ignore
  return { previews = out }
end

-- ---------------------------------------------------------------- editing advice
function Commands.get_preview(args)
  local photo = findPhoto(args.photo_id)
  local size = math.max(256, math.min(tonumber(args.size) or 768, 1280))
  local done, data, err = false, nil, nil
  -- keep `request` referenced until we are done, or Lightroom may drop the callback
  local request = photo:requestJpegThumbnail(size, size, function(d, e)
    data, err, done = d, e, true
  end)
  local waited = 0
  while not done and waited < 30 do
    LrTasks.sleep(0.1)
    waited = waited + 0.1
  end
  if not done or not data then error('preview failed: ' .. tostring(err or 'timeout'), 0) end
  local encoded = LrStringUtils.encodeBase64(data)
  request = nil
  return { image_base64 = encoded }
end

function Commands.get_develop_settings(args)
  local photo = findPhoto(args.photo_id)
  local out = {}
  for k, v in pairs(photo:getDevelopSettings() or {}) do
    local t = type(v)
    if t == 'number' or t == 'string' or t == 'boolean' then out[k] = v end
  end
  return { settings = out }
end

-- Presets made by create_preset. Lightroom returns a handle but does not list them in
-- developPresetFolders(), so apply_preset could never find them by name; keep the handles.
local createdPresets = {}

local function allPresets()
  local out = {}
  for _, folder in ipairs(LrApplication.developPresetFolders() or {}) do
    for _, preset in ipairs(folder:getDevelopPresets() or {}) do
      out[#out + 1] = { folder = folder:getName(), name = preset:getName(), preset = preset }
    end
  end
  return out
end

function Commands.list_presets()
  local out = {}
  for _, p in ipairs(allPresets()) do out[#out + 1] = { folder = p.folder, name = p.name } end
  return { presets = out }
end

local function findPreset(wanted)
  local hits = {}
  for _, p in ipairs(allPresets()) do
    if wanted == p.name or wanted == (p.folder .. '/' .. p.name) then hits[#hits + 1] = p end
  end
  if #hits == 0 and createdPresets[wanted] then return createdPresets[wanted] end
  if #hits == 0 then notFound('Preset not found: ' .. tostring(wanted)) end
  if #hits > 1 then notFound('Preset name is ambiguous, use Folder/Name: ' .. tostring(wanted)) end
  return hits[1].preset
end

function Commands.apply_preset(args)
  local photos = findAll(args.photo_ids)
  local preset = findPreset(args.preset)
  writeAccess('MHPhotos: apply preset', function()
    for _, p in ipairs(photos) do p:applyDevelopPreset(preset, _PLUGIN) end
  end)
  return { updated = #photos }
end

local function checkSettings(settings)
  if type(settings) ~= 'table' or next(settings) == nil then error('settings required', 0) end
  for k, v in pairs(settings) do
    local t = type(v)
    if type(k) ~= 'string' or (t ~= 'number' and t ~= 'boolean') then
      error('settings must map names to numbers or booleans', 0)
    end
  end
end

function Commands.apply_develop_settings(args)
  checkSettings(args.settings)
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: develop settings', function()
    for _, p in ipairs(photos) do p:applyDevelopSettings(args.settings, 'MHPhotos: develop settings') end
  end)
  return { updated = #photos }
end

function Commands.create_preset(args)
  checkSettings(args.settings)
  local name = args.name
  if type(name) ~= 'string' or name == '' or #name > 80 then error('preset name required', 0) end
  local count = 0
  for _ in pairs(args.settings) do count = count + 1 end
  if _PLUGIN == nil then error('Lightroom gave no plugin handle (_PLUGIN), cannot create a preset', 0) end
  local returned = LrApplication.addDevelopPresetForPlugin(_PLUGIN, name, args.settings)
  -- Report what Lightroom did: a preset that is not listed afterwards cannot be applied by name.
  if returned ~= nil then createdPresets[name] = returned end
  local listed = false
  for _, p in ipairs(allPresets()) do
    if p.name == name then listed = true; break end
  end
  return {
    created = name, folder = 'MHPhotos MCP Bridge', parameters = count,
    returned_by_lightroom = returned ~= nil, listed_in_presets = listed,
  }
end

-- ---------------------------------------------------------------- organising (writes)
local LABELS = { none = true, red = true, yellow = true, green = true, blue = true, purple = true }

-- Collection set for a path like "2026" or "2026/Reisen": created level by level when missing.
local function ensureSet(cat, path)
  if path == nil or path == '' then return nil end
  if type(path) ~= 'string' or #path > 240 then error('parent must be a text path like "2026" or "2026/Reisen"', 0) end
  local set = nil
  for part in string.gmatch(path, '[^/]+') do
    if #part > 120 then error('collection set name too long', 0) end
    set = cat:createCollectionSet(part, set, true) -- true: reuse the existing set of that name
    if not set then error('Lightroom did not create the collection set ' .. part, 0) end
  end
  return set
end

function Commands.create_collection(args)
  local name = args.name
  if type(name) ~= 'string' or name == '' or #name > 120 then error('collection name required', 0) end
  local coll
  writeAccess('MHPhotos: create collection', function()
    local cat = catalog()
    local parent = ensureSet(cat, args.parent)
    coll = cat:createCollection(name, parent, true) -- true: return the existing one of that name
  end)
  if not coll then error('Lightroom did not create the collection', 0) end
  return { id = tostring(coll.localIdentifier), name = name, parent = args.parent }
end

-- Moves an existing collection (or collection set) into the set at path "parent" ('' = top level).
function Commands.move_collection(args)
  local cat = catalog()
  local coll = cat:getCollectionByLocalIdentifier(tonumber(args.collection_id))
  if not coll then notFound('Collection not found: ' .. tostring(args.collection_id)) end
  if not coll.setParent then error('this Lightroom version cannot move collections; drag it by hand', 0) end
  local moved
  writeAccess('MHPhotos: move collection', function()
    local parent = ensureSet(cat, args.parent)
    local ok, err = LrTasks.pcall(function() coll:setParent(parent) end)
    if not ok then error('Lightroom could not move the collection: ' .. tostring(err), 0) end
    moved = true
  end)
  return { id = tostring(coll.localIdentifier), parent = args.parent or '', moved = moved == true }
end

function Commands.add_to_collection(args)
  local coll = catalog():getCollectionByLocalIdentifier(tonumber(args.collection_id))
  if not coll then notFound('Collection not found: ' .. tostring(args.collection_id)) end
  if not coll.addPhotos then error('this id is a collection set; pick a collection inside it', 0) end
  if coll.isSmartCollection and coll:isSmartCollection() then
    error('smart collections cannot receive photos manually', 0)
  end
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: add to collection', function() coll:addPhotos(photos) end)
  return { updated = #photos }
end

function Commands.remove_from_collection(args)
  -- Takes photos out of an album only; the photos stay in the catalog and in every other album.
  local coll = catalog():getCollectionByLocalIdentifier(tonumber(args.collection_id))
  if not coll then notFound('Collection not found: ' .. tostring(args.collection_id)) end
  if not coll.removePhotos then error('this id is a collection set; pick a collection inside it', 0) end
  if coll.isSmartCollection and coll:isSmartCollection() then
    error('smart collections are filled by their rules; change the rules instead', 0)
  end
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: remove from collection', function() coll:removePhotos(photos) end)
  return { updated = #photos }
end

function Commands.set_color_label(args)
  if not LABELS[args.label] then error('label must be none, red, yellow, green, blue or purple', 0) end
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: color label', function()
    for _, p in ipairs(photos) do p:setRawMetadata('colorNameForLabel', args.label) end
  end)
  return { updated = #photos }
end

function Commands.set_title_caption(args)
  if args.title == nil and args.caption == nil then error('title or caption required', 0) end
  for _, v in ipairs({ args.title or '', args.caption or '' }) do
    if type(v) ~= 'string' or #v > 4000 then error('title/caption must be text (max 4000 bytes)', 0) end
  end
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: title/caption', function()
    for _, p in ipairs(photos) do
      if args.title ~= nil then p:setRawMetadata('title', args.title) end
      if args.caption ~= nil then p:setRawMetadata('caption', args.caption) end
    end
  end)
  return { updated = #photos }
end

-- Only used by undo: removes keywords (by name) that this server had added.
function Commands.remove_keywords(args)
  local names = {}
  for _, n in ipairs(args.keywords or {}) do names[#names + 1] = tostring(n):lower() end
  if #names == 0 then error('keywords required', 0) end
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: remove keywords', function()
    for _, p in ipairs(photos) do
      for _, kw in ipairs(p:getRawMetadata('keywords') or {}) do
        local kwName = kw:getName():lower()
        for _, n in ipairs(names) do
          if kwName == n then p:removeKeyword(kw); break end
        end
      end
    end
  end)
  return { updated = #photos }
end

-- ---------------------------------------------------------------- virtual copies (jobs, since 0.1.25)
-- What Lightroom Classic does (06.11.2026, plug-in 0.1.21 to 0.1.24):
-- * catalog:createVirtualCopies makes the copy but never comes back to the calling task (lrc_app.log ends with
--   ag.library.createVirtualCopy, "session pending=true"). 0.1.21/0.1.23 called it in the polling task and lost the
--   bridge; 0.1.24 called it in a helper task.
-- * For about 5 minutes afterwards every read of the NEW copy (uuid, metadata, size) takes about 2 minutes; older
--   photos answer at once. Most likely the copy belongs to the Lightroom session that never closed.
-- * 0.1.24 waited "20 s" by counting 40 rounds of sleep(0.5), but each round read the new copy (uuid) and the next
--   photo's selection ran while Lightroom was still busy, so one round could take minutes: 8 photos hung > 13 minutes
--   in the polling task and the server dropped the bridge.
-- Since 0.1.25 nothing of that runs in the polling task:
-- * create_virtual_copies only checks the ids and starts a JOB (a worker task) and answers at once with its id.
-- * The worker selects ALL photos together and calls createVirtualCopies ONCE (one hanging Lightroom call per batch,
--   not one per photo), in a helper task of its own, then watches the originals' virtualCopies, matched by
--   localIdentifier (a plain property, no catalog read of the new copy), against a WALL CLOCK deadline. Only then it
--   reads the new copies' uuids; slow, but in the worker.
-- * virtual_copy_job answers from memory only (no catalog call), so it can never block.
-- * Commands on a copy made in the last minutes first probe it in a helper task for at most PROBE_WAIT seconds; while
--   Lightroom is still finishing it they answer photo_busy at once instead of blocking the bridge for 2 minutes.
-- Lightroom only creates virtual copies of the SELECTED photos, and a selection can only contain photos of the active
-- source (and works reliably in the Library grid): grid view, source "All Photographs", select exactly these photos.
-- The selection is proven before the copy is made; the Lightroom selection/view changes as a side effect.
local COPY_WAIT = 20        -- s the worker watches for the copies after starting the call (plus 3 s per extra photo)
local SELECT_WAIT = 10      -- s for the selection to show the requested photos
local CALL_SETTLE = 360     -- s after a call that never returned before another one may start
local FRESH_TTL = 360       -- s a new copy counts as "still being finished" until a probe has read it
local PROBE_WAIT = 8        -- s the polling task waits for a probe of such a copy
local JOB_KEEP = 3600       -- s a finished job stays queryable

local function clock() return LrDate.currentTime() end

local jobs, jobSeq, lastJob = {}, 0, nil
local fresh = {}            -- uuid of a new copy -> time it was made
local probes = {}           -- uuid -> probe in flight { done, ok, err }
Commands._vc = { jobs = jobs, fresh = fresh, probes = probes } -- for the tests

local function selectAll(photos)
  local cat = catalog()
  LrTasks.pcall(function() LrApplicationView.switchToModule('library') end)
  LrTasks.pcall(function() LrApplicationView.gridView() end)
  LrTasks.pcall(function() cat:setActiveSources(cat.kAllPhotos) end)
  local want = {}
  for _, p in ipairs(photos) do want[p.localIdentifier] = true end
  local rest = {}
  for i = 2, #photos do rest[#rest + 1] = photos[i] end
  local deadline = clock() + SELECT_WAIT
  repeat
    cat:setSelectedPhotos(photos[1], rest)
    local got = cat.getTargetPhotos and cat:getTargetPhotos() or { cat:getTargetPhoto() }
    local n, ok = 0, true
    for _, t in ipairs(got or {}) do
      n = n + 1
      if not want[t.localIdentifier] then ok = false end
    end
    if ok and n == #photos then return true end
    LrTasks.sleep(0.5)
  until clock() >= deadline
  return false
end

-- auto_tone works on one selected photo at a time
local function selectOnly(photo)
  if not selectAll({ photo }) then
    error('Lightroom did not select the photo ' .. tostring(photo:getRawMetadata('uuid')) .. ' (is it visible in All Photographs?)', 0)
  end
end

-- localIdentifier -> photo of the existing virtual copies of a photo
local function copiesOf(photo)
  local out = {}
  local ok, list = LrTasks.pcall(function() return photo:getRawMetadata('virtualCopies') end)
  if ok and type(list) == 'table' then
    for _, c in ipairs(list) do out[c.localIdentifier] = c end
  end
  return out
end

local function newCopy(photo, before)
  for lid, c in pairs(copiesOf(photo)) do
    if not before[lid] then return c end
  end
  return nil
end

local function errText(e) return tostring(type(e) == 'table' and e.message or e) end

local function callCreate(job, copyName)
  job.callStarted = clock()
  LrTasks.startAsyncTask(function()
    local ok, made = LrTasks.pcall(function() return catalog():createVirtualCopies(copyName) end)
    if not ok and errText(made):lower():find('write') then
      -- only a Lightroom that insists on a gate gets the call inside one (still in this helper task)
      ok, made = LrTasks.pcall(function()
        local r
        writeAccess('MHPhotos: virtual copy', function() r = catalog():createVirtualCopies(copyName) end)
        return r
      end)
    end
    job.callOk, job.callMade, job.callDone = ok, made, true
  end, 'MHPhotos: virtual copy call')
end

local function allFound(job)
  for i = 1, #job.photos do if not job.found[i] then return false end end
  return true
end

local function lookForCopies(job)
  for i, p in ipairs(job.photos) do
    if not job.found[i] then job.found[i] = newCopy(p, job.before[i]) end
  end
  -- a Lightroom that returns the copies (as the SDK says): match them to their originals
  if not allFound(job) and job.callDone and job.callOk and type(job.callMade) == 'table' then
    for _, c in ipairs(job.callMade) do
      local okM, master = LrTasks.pcall(function() return c:getRawMetadata('masterPhoto') end)
      for i, p in ipairs(job.photos) do
        if okM and master and master.localIdentifier == p.localIdentifier and not job.found[i] then job.found[i] = c end
      end
    end
  end
end

local function finish(job, state, err)
  job.state, job.error, job.finished = state, err, clock()
end

local function readIds(job)
  job.phase = 'reading ids'
  for i, c in ipairs(job.found) do
    if c and not job.ids[i] then
      local ok, id = LrTasks.pcall(function() return c:getRawMetadata('uuid') end)
      if ok and id then
        job.ids[i] = id
        fresh[id] = clock()
      end
    end
  end
end

local function worker(job, copyName)
  if job.cancelled then return end
  job.phase = 'selecting'
  job.workerStarted = clock()
  if not selectAll(job.photos) then
    return finish(job, 'failed', 'Lightroom did not select the photos (are they visible in All Photographs?); no copy was made')
  end
  for i, p in ipairs(job.photos) do job.before[i] = copiesOf(p) end
  job.phase = 'copying'
  callCreate(job, copyName)
  local deadline = clock() + COPY_WAIT + 3 * (#job.photos - 1)
  while clock() < deadline do
    lookForCopies(job)
    if allFound(job) then break end
    if job.callDone and not job.callOk then break end
    LrTasks.sleep(0.5)
  end
  readIds(job)
  if allFound(job) then return finish(job, 'done') end
  if job.callDone and not job.callOk then
    return finish(job, 'failed', 'Lightroom created no virtual copy (' .. errText(job.callMade) .. ')')
  end
  finish(job, 'timeout', 'Lightroom did not create all virtual copies within '
    .. math.floor(deadline - job.callStarted) .. ' s (the bridge stays connected)')
  -- keep watching quietly: a copy that shows up late is still reported, and lifts copy_pending
  while not allFound(job) and clock() - job.callStarted < CALL_SETTLE do
    LrTasks.sleep(5)
    lookForCopies(job)
  end
  readIds(job)
  job.lateDone = true
end

local MANUAL = "Create the copy by hand in Lightroom (select the photo, Cmd+' / Ctrl+'), then crop it with "
  .. 'crop_to_aspect(virtual_copies=false) on the copy.'

local function snapshot(job)
  local copies, missing = {}, {}
  for i, src in ipairs(job.sourceIds) do
    if job.ids[i] then copies[#copies + 1] = { source_id = src, id = job.ids[i] }
    else missing[#missing + 1] = src end
  end
  local s = {
    job_id = job.id, state = job.state, phase = job.phase, total = #job.photos, copies = copies,
    elapsed = math.floor(clock() - job.created),
  }
  if #missing > 0 and job.state ~= 'running' then s.missing = missing end
  if job.error then
    s.error = job.error .. (#copies > 0 and ' Copies made: ' .. #copies .. '.' or '') .. ' ' .. MANUAL
  end
  return s
end

-- Is an earlier Lightroom call still open in a way that a new one could copy the wrong photos?
local function pendingReason()
  local job = lastJob
  if not job then return nil end
  if job.phase == 'queued' then
    if clock() - job.created < COPY_WAIT then return 'an earlier copy job has not started yet' end
    job.cancelled = true -- the worker never ran; if it runs later it stops at once
    finish(job, 'failed', 'the copy job never started in Lightroom')
    return nil
  end
  if job.state == 'running' then return 'copy job ' .. job.id .. ' is still running' end
  if not job.callStarted or job.callDone then return nil end
  if not allFound(job) then
    return 'the "Create Virtual Copy" of job ' .. job.id .. ' has not finished in Lightroom (if Lightroom ran it later, it '
      .. 'would copy whatever is selected then)'
  end
  local wait = CALL_SETTLE - (clock() - job.callStarted)
  if wait > 0 then
    return 'Lightroom is still finishing the copies of job ' .. job.id .. ' (try again in about '
      .. math.ceil(wait / 60) .. ' min)'
  end
  return nil
end

function Commands.create_virtual_copies(args)
  local photos = findAll(args.photo_ids)
  if #photos > 10 then error('at most 10 virtual copies per call', 0) end
  local copyName = args.copy_name
  if copyName == nil then copyName = 'MHPhotos' end
  if type(copyName) ~= 'string' or copyName == '' or #copyName > 60 then error('copy_name must be text (max 60 characters)', 0) end
  local why = pendingReason()
  if why then error('copy_pending: ' .. why .. '; no new copy was started. ' .. MANUAL, 0) end
  for id, job in pairs(jobs) do
    if job.finished and clock() - job.finished > JOB_KEEP then jobs[id] = nil end
  end
  jobSeq = jobSeq + 1
  local sourceIds = {}
  for i, id in ipairs(args.photo_ids) do sourceIds[i] = tostring(id) end
  local job = {
    id = 'vc' .. jobSeq .. '-' .. tostring(math.floor(clock())), photos = photos, sourceIds = sourceIds,
    before = {}, found = {}, ids = {}, state = 'running', phase = 'queued', created = clock(),
  }
  jobs[job.id], lastJob = job, job
  invalidateCache()
  LrTasks.startAsyncTask(function()
    local ok, err = LrTasks.pcall(worker, job, copyName)
    if not ok and job.state == 'running' then finish(job, 'failed', errText(err)) end
  end, 'MHPhotos: virtual copies')
  return snapshot(job)
end

-- Read only, from memory: never touches the catalog, so it always answers at once.
function Commands.virtual_copy_job(args)
  local job = jobs[tostring(args.job_id)]
  if not job then notFound('no virtual copy job ' .. tostring(args.job_id) .. ' (Lightroom restarted?)') end
  return snapshot(job)
end

-- Reads a new copy in a helper task (the same fields get_photo reads) and waits at most PROBE_WAIT seconds. True when
-- Lightroom answered, so the command itself will not hang on it.
local function probeFresh(id)
  local pr = probes[id]
  if not pr then
    pr = {}
    probes[id] = pr
    LrTasks.startAsyncTask(function()
      local ok, err = LrTasks.pcall(function()
        local p = catalog():findPhotoByUuid(id)
        if p then
          photoMetadata(p)
          p:getRawMetadata('croppedDimensions')
        end
      end)
      pr.ok, pr.err, pr.done = ok, err, true
    end, 'MHPhotos: probe new copy')
  end
  local deadline = clock() + PROBE_WAIT
  while not pr.done and clock() < deadline do LrTasks.sleep(0.2) end
  if pr.done then probes[id] = nil end
  return pr.done
end

local function freshIds(args)
  local out = {}
  local function add(id)
    local t = id ~= nil and fresh[tostring(id)]
    if t then
      if clock() - t < FRESH_TTL then out[#out + 1] = tostring(id) else fresh[tostring(id)] = nil end
    end
  end
  add(args.photo_id)
  if type(args.photo_ids) == 'table' then for _, id in ipairs(args.photo_ids) do add(id) end end
  return out
end

local function guardFresh(name, args)
  if name == 'virtual_copy_job' or name == 'create_virtual_copies' then return end
  for _, id in ipairs(freshIds(args)) do
    if probeFresh(id) then
      fresh[id] = nil
    else
      error('photo_busy: Lightroom is still finishing the new virtual copy ' .. id .. ' (for a few minutes after a copy '
        .. 'is made, reading it takes minutes). Nothing was changed; try again in a minute.', 0)
    end
  end
end

-- Read only: size of each picture after rotation and crop (croppedDimensions); the server uses it to
-- compute a crop for a social format.
function Commands.photo_dimensions(args)
  local out = {}
  for i, photo in ipairs(findAll(args.photo_ids)) do
    local c = photo:getRawMetadata('croppedDimensions')
    local f = photo:getRawMetadata('dimensions')
    local d = c or f
    if type(d) == 'table' and d.width and d.height then
      local rec = { width = d.width, height = d.height }
      if type(f) == 'table' and f.width and f.height then rec.full_width, rec.full_height = f.width, f.height end
      local ok, o = LrTasks.pcall(function() return photo:getRawMetadata('orientation') end)
      if ok and type(o) == 'string' then rec.orientation = o end
      out[args.photo_ids[i]] = rec
    end
  end
  return { dimensions = out }
end

-- ---------------------------------------------------------------- catalog audit
-- Read only: one page of the whole catalog with path, size, capture time and camera (Audit.lua).
function Commands.audit_page(args) return Audit.page(args) end

-- Read only: one step of the capture-year scan (progress for the server, stops between steps).
function Commands.audit_scan(args) return Audit.scan(args) end

-- Read only: photos captured in a date range, with rating, flag, place, GPS, title/caption presence.
function Commands.photos_by_date(args) return Audit.range(args) end

-- Write: GPS position and place names. GPS is written where this Lightroom allows it; the text
-- fields are written either way, and the result says whether GPS was accepted.
local PLACE_KEYS = { 'location', 'city', 'stateProvince', 'country' }

function Commands.set_location(args)
  local lat, lon = tonumber(args.latitude), tonumber(args.longitude)
  if (lat == nil) ~= (lon == nil) then error('latitude and longitude go together', 0) end
  if lat and (lat < -90 or lat > 90 or lon < -180 or lon > 180) then error('latitude/longitude out of range', 0) end
  local any = lat ~= nil
  for _, k in ipairs(PLACE_KEYS) do
    local v = args[k]
    if v ~= nil and (type(v) ~= 'string' or #v > 200) then error(k .. ' must be text (max 200 bytes)', 0) end
    if v ~= nil then any = true end
  end
  if not any then error('nothing to set', 0) end
  local photos = findAll(args.photo_ids)
  local gpsWritten, gpsError = lat ~= nil, nil
  writeAccess('MHPhotos: Ort', function()
    for _, p in ipairs(photos) do
      if lat then
        local ok, err = LrTasks.pcall(function() p:setRawMetadata('gps', { latitude = lat, longitude = lon }) end)
        if not ok then gpsWritten, gpsError = false, tostring(err) end
      end
      for _, k in ipairs(PLACE_KEYS) do
        if args[k] ~= nil then p:setRawMetadata(k, args[k]) end
      end
    end
  end)
  return { updated = #photos, gps_written = gpsWritten, gps_error = gpsError }
end

-- Write: rotate photos clockwise by 90, 180 or 270 degrees (LrPhoto:rotateRight / rotateLeft). Each photo is
-- tried on its own, a refusal is reported and does not stop the others. Turning back is another rotation.
function Commands.rotate_photos(args)
  local degrees = tonumber(args.degrees)
  if degrees ~= 90 and degrees ~= 180 and degrees ~= 270 then
    error('degrees must be 90, 180 or 270 (clockwise)', 0)
  end
  local photos = findAll(args.photo_ids)
  local rotated, failed, firstError = 0, 0, nil
  writeAccess('MHPhotos: Drehen', function()
    for _, p in ipairs(photos) do
      local ok, err = LrTasks.pcall(function()
        if degrees == 270 then
          p:rotateLeft()
        else
          for _ = 1, math.floor(degrees / 90) do p:rotateRight() end
        end
      end)
      if ok then rotated = rotated + 1 else failed, firstError = failed + 1, firstError or tostring(err) end
    end
  end)
  return { updated = #photos, rotated = rotated, failed = failed, error = firstError }
end

-- Read only: the file path of each photo (the server writes a capture time into JPEG files that way).
function Commands.photo_paths(args)
  local paths = {}
  for i, photo in ipairs(findAll(args.photo_ids)) do
    paths[args.photo_ids[i]] = photo:getRawMetadata('path')
  end
  return { paths = paths }
end

-- Write (additive): review collections below one collection set, e.g.
-- Prüfen / Ordner / "→ 2021-07-14". Photos deleted since the audit are skipped and counted.
local MAX_REVIEW_PHOTOS, MAX_REVIEW_GROUPS = 5000, 300

-- Smart collection from simple rules (same filters as list_photos; the index does the matching).
function Commands.create_smart_collection(args)
  local name = args.name
  if type(name) ~= 'string' or name == '' or #name > 120 then error('collection name required', 0) end
  local rules = args.rules or {}
  local desc = searchDescFor(rules, false)
  if not desc then error('rules needed: min_rating, keyword, captured_from/captured_to or flag', 0) end
  local coll
  writeAccess('MHPhotos: create smart collection', function()
    local cat = catalog()
    if not cat.createSmartCollection then error('this Lightroom version cannot create smart collections', 0) end
    local parent = ensureSet(cat, args.parent)
    coll = cat:createSmartCollection(name, desc, parent, true)
  end)
  if not coll then error('Lightroom did not create the smart collection', 0) end
  return { id = tostring(coll.localIdentifier), name = name, parent = args.parent }
end

-- Smart previews keep editing and contact sheets working without the original (NAS offline).
function Commands.build_smart_previews(args)
  local photos = findAll(args.photo_ids)
  local cat = catalog()
  if not cat.buildSmartPreviews then error('this Lightroom version cannot build smart previews from a plug-in', 0) end
  local ok, err = LrTasks.pcall(function() cat:buildSmartPreviews(photos) end)
  if not ok then error('smart previews failed: ' .. tostring(err), 0) end
  return { requested = #photos }
end

-- Adds one file to the catalog (like Import without moving it). Optional: into a collection.
function Commands.add_photo(args)
  local LrFileUtils = import 'LrFileUtils'
  local path = args.path
  if type(path) ~= 'string' or path == '' or #path > 1000 or path:find('..', 1, true) or path:sub(1, 1) ~= '/' then
    error('absolute file path required', 0)
  end
  if LrFileUtils.exists(path) ~= 'file' then error('file not found: ' .. path, 0) end
  local photo
  writeAccess('MHPhotos: add photo to catalog', function()
    photo = catalog():addPhoto(path)
    if photo and args.collection_id then
      local coll = catalog():getCollectionByLocalIdentifier(tonumber(args.collection_id))
      if coll and coll.addPhotos then coll:addPhotos({ photo }) end
    end
  end)
  if not photo then error('Lightroom did not add the file', 0) end
  return { id = photo:getRawMetadata('uuid'), filename = photo:getFormattedMetadata('fileName') }
end

-- ---------------------------------------------------------------- own metadata fields (MetadataDefinition.lua)
local STATUS_VALUES = { insta_kandidat = true, fotobuch = true, veroeffentlicht = true, aussortiert = true }

-- Sets the plug-in's own fields: status (one of STATUS_VALUES, or '' to clear) and/or note (text).
function Commands.set_custom_metadata(args)
  local status, note = args.status, args.note
  if status == nil and note == nil then error('status or note required', 0) end
  if status ~= nil and status ~= '' and not STATUS_VALUES[status] then
    error('status must be insta_kandidat, fotobuch, veroeffentlicht, aussortiert or empty', 0)
  end
  if note ~= nil and (type(note) ~= 'string' or #note > 1000) then error('note must be text (max 1000 bytes)', 0) end
  local photos = findAll(args.photo_ids)
  local failure
  writeAccess('MHPhotos: own metadata', function()
    for _, p in ipairs(photos) do
      local ok, err = LrTasks.pcall(function()
        if status ~= nil then p:setPropertyForPlugin(_PLUGIN, 'status', status ~= '' and status or nil) end
        if note ~= nil then p:setPropertyForPlugin(_PLUGIN, 'notiz', note ~= '' and note or nil) end
      end)
      if not ok then failure = tostring(err); return end
    end
  end)
  if failure then
    error('own fields are not active in this plug-in version (no metadata provider loaded): ' .. failure, 0)
  end
  return { updated = #photos }
end

-- Lightroom's own "Auto" for tone and (optionally) white balance. Only works on the photo shown in the
-- Develop module, so one photo after the other; returns to the Library grid at the end.
function Commands.auto_tone(args)
  local LrDevelopController = import 'LrDevelopController'
  if not LrDevelopController.setAutoTone then error('this Lightroom version has no auto tone for plug-ins', 0) end
  local photos = findAll(args.photo_ids)
  if #photos > 10 then error('at most 10 photos per call', 0) end
  local done = 0
  for _, photo in ipairs(photos) do
    selectOnly(photo)
    LrTasks.pcall(function() LrApplicationView.switchToModule('develop') end)
    LrTasks.sleep(0.5)
    local ok, err = LrTasks.pcall(function()
      LrDevelopController.setAutoTone()
      if args.white_balance and LrDevelopController.setAutoWhiteBalance then LrDevelopController.setAutoWhiteBalance() end
    end)
    LrTasks.sleep(0.5)
    if not ok then
      LrTasks.pcall(function() LrApplicationView.switchToModule('library') end)
      error('auto tone failed on ' .. tostring(photo:getRawMetadata('uuid')) .. ': ' .. tostring(err), 0)
    end
    done = done + 1
  end
  LrTasks.pcall(function() LrApplicationView.switchToModule('library') end)
  return { updated = done, white_balance = args.white_balance == true }
end

-- ---------------------------------------------------------------- webhooks (addresses only from webhooks.json)
local function loadHooks()
  local Webhook = require 'Webhook'
  local Json = require 'Json'
  local LrPathUtils = import 'LrPathUtils'
  local LrFileUtils = import 'LrFileUtils'
  local path = LrPathUtils.child(LrPathUtils.child(LrPathUtils.getStandardFilePath('home'), '.mhphotos-lightroom-mcp'), 'webhooks.json')
  -- readFile raises an error for a missing file instead of returning nil
  local raw
  if LrFileUtils.exists(path) == 'file' then
    local okRead, content = pcall(LrFileUtils.readFile, path)
    if okRead then raw = content end
  end
  if not raw then return {}, { 'no webhooks.json in ~/.mhphotos-lightroom-mcp' } end
  local ok, decoded = pcall(Json.decode, raw)
  if not ok then return {}, { 'webhooks.json is not valid JSON' } end
  return Webhook.normalize(decoded)
end

function Commands.webhook_list()
  local Webhook = require 'Webhook'
  local hooks, problems = loadHooks()
  return { hooks = Webhook.names(hooks), problems = problems }
end

function Commands.webhook_send(args)
  local Webhook = require 'Webhook'
  local Json = require 'Json'
  local LrHttp = import 'LrHttp'
  local hooks = loadHooks()
  if type(args.name) ~= 'string' then error('webhook name required', 0) end
  return Webhook.send(hooks, args.name, args.payload, {
    encode = Json.encode,
    -- LrHttp.post returns (body, headersTable); the HTTP status is headersTable.status
    post = function(url, body, headers, timeout)
      local response, hdrs = LrHttp.post(url, body, headers, 'POST', timeout)
      return response, hdrs and hdrs.status
    end,
  })
end

local function validName(v) return type(v) == 'string' and v ~= '' and #v <= 120 end

function Commands.create_review_collections(args)
  if not validName(args.root) then error('root set name required (max 120 characters)', 0) end
  local groups = args.groups
  if type(groups) ~= 'table' or #groups == 0 or #groups > MAX_REVIEW_GROUPS then
    error('1-' .. MAX_REVIEW_GROUPS .. ' groups required', 0)
  end
  local cat, total, missing, resolved = catalog(), 0, 0, {}
  for i, g in ipairs(groups) do
    if not validName(g.set) or not validName(g.name) then
      error('every group needs set and name (max 120 characters)', 0)
    end
    total = total + #(g.photo_ids or {})
    if total > MAX_REVIEW_PHOTOS then error('at most ' .. MAX_REVIEW_PHOTOS .. ' photos per call', 0) end
    local photos = {}
    for _, id in ipairs(g.photo_ids or {}) do
      local p = cat:findPhotoByUuid(id)
      if p then photos[#photos + 1] = p else missing = missing + 1 end
    end
    resolved[i] = photos
  end
  local made = {}
  writeAccess('MHPhotos: Prüf-Sammlungen', function()
    -- true: reuse an existing set/collection of that name (a second run adds to it)
    local rootSet = cat:createCollectionSet(args.root, nil, true)
    local sets = {}
    for i, g in ipairs(groups) do
      sets[g.set] = sets[g.set] or cat:createCollectionSet(g.set, rootSet, true)
      local coll = cat:createCollection(g.name, sets[g.set], true)
      if #resolved[i] > 0 then coll:addPhotos(resolved[i]) end
      made[#made + 1] = { set = g.set, name = g.name, added = #resolved[i] }
    end
  end)
  return { collections = made, missing = missing }
end

-- ---------------------------------------------------------------- keywords (since 0.1.26)
-- keyword_tree lists every keyword (flat, with parent and path) page by page, optionally with the number of photos;
-- merge_keywords moves the photos of duplicate keywords onto one target keyword. The SDK cannot delete a keyword:
-- an emptied keyword is removed in Lightroom with Metadata > Purge Unused Keywords.
local KEYWORD_PAGE = 500
local KEYWORD_TTL = 300
local MERGE_MAX = 5000
local keywordCache

local function keywordId(kw) return tostring(kw.localIdentifier) end

local function flattenKeywords(refresh)
  local now = os.time()
  if not refresh and keywordCache and now - keywordCache.at < KEYWORD_TTL then return keywordCache.list end
  local list, byId = {}, {}
  local function walk(kws, parent, prefix, depth)
    for _, kw in ipairs(kws or {}) do
      local name = kw:getName()
      local path = prefix and (prefix .. ' > ' .. name) or name
      local okC, children = LrTasks.pcall(function() return kw:getChildren() end)
      children = okC and type(children) == 'table' and children or {}
      local entry = { kw = kw, id = keywordId(kw), name = name, path = path, depth = depth,
        parent = parent and keywordId(parent) or nil, children = #children }
      list[#list + 1] = entry
      byId[entry.id] = entry
      walk(children, kw, path, depth + 1)
    end
  end
  walk(catalog():getKeywords(), nil, nil, 0)
  keywordCache = { at = now, list = list, byId = byId }
  return list
end

-- args: offset (default 0; 0 re-reads the keyword list), limit (max 500), counts (true: number of photos per keyword)
function Commands.keyword_tree(args)
  local offset = math.max(0, math.floor(tonumber(args.offset) or 0))
  local limit = math.max(1, math.min(math.floor(tonumber(args.limit) or KEYWORD_PAGE), KEYWORD_PAGE))
  local list = flattenKeywords(offset == 0 or args.refresh == true)
  local out = { total = #list, offset = offset, next_offset = offset, keywords = {} }
  for i = offset + 1, math.min(offset + limit, #list) do
    local e = list[i]
    local okS, syn = LrTasks.pcall(function() return e.kw:getSynonyms() end)
    local item = { id = e.id, name = e.name, path = e.path, depth = e.depth, parent = e.parent, children = e.children,
      synonyms = okS and type(syn) == 'table' and #syn > 0 and syn or nil }
    if args.counts == true then
      local okP, ps = LrTasks.pcall(function() return e.kw:getPhotos() end)
      item.photos = okP and type(ps) == 'table' and #ps or nil
    end
    out.keywords[#out.keywords + 1] = item
    out.next_offset = i
  end
  return out
end

-- args: source_ids (keyword ids from keyword_tree), target_id, max_photos (default and max 5000 per call).
-- Each photo of a source keyword gets the target keyword and loses the source keyword. Returns moved and remaining
-- (photos still on the sources; call again until remaining is 0).
function Commands.merge_keywords(args)
  local sources = args.source_ids
  if type(sources) ~= 'table' or #sources == 0 or #sources > 50 then error('1-50 source_ids required', 0) end
  flattenKeywords(true)
  local target = keywordCache.byId[tostring(args.target_id)]
  if not target then notFound('Keyword not found: ' .. tostring(args.target_id)) end
  local resolved = {}
  for _, id in ipairs(sources) do
    local e = keywordCache.byId[tostring(id)]
    if not e then notFound('Keyword not found: ' .. tostring(id)) end
    if e.id == target.id then error('the target keyword must not be a source', 0) end
    resolved[#resolved + 1] = e
  end
  local budget = math.max(1, math.min(math.floor(tonumber(args.max_photos) or MERGE_MAX), MERGE_MAX))
  local work, remaining = {}, 0
  for _, e in ipairs(resolved) do
    local photos = e.kw:getPhotos() or {}
    for _, p in ipairs(photos) do
      if budget > 0 then work[#work + 1] = { p, e.kw }; budget = budget - 1 else remaining = remaining + 1 end
    end
  end
  if #work > 0 then
    writeAccess('MHPhotos: Stichwörter zusammenführen', function()
      for _, w in ipairs(work) do
        w[1]:addKeyword(target.kw)
        w[1]:removeKeyword(w[2])
      end
    end)
  end
  keywordCache = nil
  return { moved = #work, remaining = remaining, target = target.path }
end

-- ---------------------------------------------------------------- self test (read only)
-- Exercises every read command on a few photos and reports each result, so API mismatches
-- show up as exact messages instead of guesswork. Never changes anything.
function Commands.selftest()
  local checks = {}
  local function check(name, fn)
    local t0 = LrDate.currentTime()
    local ok, res = LrTasks.pcall(fn)
    local detail = res
    if not ok then
      detail = type(res) == 'table' and ((res.code or '') .. ' ' .. tostring(res.message)) or tostring(res)
    end
    checks[#checks + 1] = {
      name = name, ok = ok, detail = tostring(detail or ''),
      ms = math.floor((LrDate.currentTime() - t0) * 1000),
    }
    return ok, res
  end

  local env = 'unknown'
  LrTasks.pcall(function() env = 'Lightroom ' .. tostring(LrApplication.versionString()) end)

  local first = {}
  check('catalog access', function() return #catalog():getAllPhotos() .. ' photos in catalog' end)
  check('list_collections', function()
    return #Commands.list_collections({}).collections .. ' collections'
  end)
  check('list_collections with counts', function()
    return #Commands.list_collections({ include_counts = true }).collections .. ' collections counted'
  end)
  check('list_photos', function()
    local r = Commands.list_photos({ limit = 3 })
    first = r.photos
    return r.total .. ' total, ' .. #r.photos .. ' listed'
  end)
  check('list_photos with filters', function()
    local r = Commands.list_photos({ limit = 1, filters = { min_rating = 1, captured_from = '1990-01-01' } })
    return r.total .. ' photos rated 1+ with capture date'
  end)
  if #first == 0 then
    checks[#checks + 1] = { name = 'photo checks', ok = false, detail = 'catalog has no photos to test', ms = 0 }
  else
    for i = 1, #first do
      check('get_photo #' .. i, function()
        local m = Commands.get_photo({ photo_id = first[i].id })
        return m.filename .. ' flag=' .. tostring(m.flag) .. ' keywords=' .. #m.keywords
          .. ' camera=' .. tostring(m.camera) .. ' location=' .. tostring(m.location)
          .. ' gps=' .. tostring(m.gps ~= nil) .. ' iso=' .. tostring(m.iso)
      end)
    end
    check('get_preview (256 px)', function()
      return #Commands.get_preview({ photo_id = first[1].id, size = 256 }).image_base64 .. ' base64 characters'
    end)
    check('get_develop_settings', function()
      local n = 0
      for _ in pairs(Commands.get_develop_settings({ photo_id = first[1].id }).settings) do n = n + 1 end
      return n .. ' scalar settings'
    end)
  end
  check('list_presets', function() return #Commands.list_presets().presets .. ' presets' end)
  check('audit_page', function()
    local r = Commands.audit_page({ limit = 3 })
    local unsupported = #r.unsupported > 0 and (' (not in this Lightroom: ' .. table.concat(r.unsupported, ', ') .. ')') or ''
    local first = r.photos[1]
    return r.total .. ' photos, ' .. #r.photos .. ' read' .. (first and (' first capture=' .. tostring(first.t)) or '') .. unsupported
  end)

  -- API surface: do the functions that the WRITE commands need exist in this Lightroom? (nothing is called)
  local function has(label, owner, names)
    check('API present: ' .. label, function()
      local missing = {}
      for _, n in ipairs(names) do
        if type(owner[n]) ~= 'function' then missing[#missing + 1] = n end
      end
      if #missing > 0 then error('missing: ' .. table.concat(missing, ', '), 0) end
      return #names .. ' functions'
    end)
  end
  has('catalog', catalog(), { 'withWriteAccessDo', 'createCollection', 'getCollectionByLocalIdentifier',
    'createKeyword', 'createVirtualCopies', 'setSelectedPhotos', 'setActiveSources', 'findPhotoByUuid',
    'createCollectionSet', 'batchGetRawMetadata', 'batchGetFormattedMetadata' })
  has('LrApplication', LrApplication, { 'developPresetFolders', 'addDevelopPresetForPlugin' })
  has('LrApplicationView', LrApplicationView, { 'switchToModule', 'gridView' })
  if first[1] then
    local photo = catalog():findPhotoByUuid(first[1].id)
    has('photo', photo, { 'setRawMetadata', 'addKeyword', 'removeKeyword', 'applyDevelopPreset',
      'applyDevelopSettings', 'requestJpegThumbnail', 'getDevelopSettings' })
  end
  check('API present: LrExportSession', function()
    local ok, mod = pcall(import, 'LrExportSession')
    if not ok or type(mod) ~= 'function' and type(mod) ~= 'table' then error('cannot import LrExportSession', 0) end
    return 'importable (export setting keys are only verified by a real export)'
  end)

  local failed = 0
  for _, c in ipairs(checks) do if not c.ok then failed = failed + 1 end end
  return { environment = env, checks = checks, passed = #checks - failed, failed = failed }
end

function Commands.run(name, args)
  local fn = Commands[name]
  if name == 'run' or type(fn) ~= 'function' or name:sub(1, 1) == '_' then error('unknown command: ' .. tostring(name), 0) end
  args = args or {}
  guardFresh(name, args)
  return fn(args)
end

return Commands
