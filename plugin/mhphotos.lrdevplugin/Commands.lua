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
  local session = LrExportSession({
    photosToExport = photos,
    exportSettings = {
      LR_format = 'JPEG',
      LR_jpeg_quality = 0.9,
      LR_export_destinationType = 'specificFolder',
      LR_export_destinationPathPrefix = dest,
      LR_export_useSubfolder = false,
      LR_collisionHandling = 'rename',
      LR_size_doConstrain = false,
      LR_reimportExportedPhoto = false,
      LR_renamingTokensOn = false,
    },
  })
  session:doExportOnCurrentTask()
  -- Names are the expected output names; Lightroom renames on collision.
  return { exported = names, destination = dest }
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

-- Lightroom only creates virtual copies of the SELECTED photos, and a selection can only contain
-- photos of the active source (and works reliably in the Library grid). So: grid view, source
-- "All Photographs", select exactly one photo, create its copy. One copy per call keeps every
-- copy unambiguously tied to its source. The Lightroom selection/view changes as a side effect.
-- Lightroom copies the SELECTED photo, so the selection must be proven, not assumed: if it silently stays
-- on another photo, the copy would be made of that one.
local function selectOnly(photo)
  local cat = catalog()
  LrTasks.pcall(function() LrApplicationView.switchToModule('library') end)
  LrTasks.pcall(function() LrApplicationView.gridView() end)
  LrTasks.pcall(function() cat:setActiveSources(cat.kAllPhotos) end)
  local want = photo:getRawMetadata('uuid')
  for _ = 1, 6 do
    cat:setSelectedPhotos(photo, {})
    local target = cat.getTargetPhoto and cat:getTargetPhoto() or nil
    if target and target:getRawMetadata('uuid') == want then return end
    LrTasks.sleep(0.5)
  end
  error('Lightroom did not select the photo ' .. tostring(want) .. ' (is it visible in All Photographs?); no copy was made', 0)
end

function Commands.create_virtual_copies(args)
  local photos = findAll(args.photo_ids)
  if #photos > 10 then error('at most 10 virtual copies per call', 0) end
  local copyName = args.copy_name
  if copyName == nil then copyName = 'MHPhotos' end
  if type(copyName) ~= 'string' or copyName == '' or #copyName > 60 then error('copy_name must be text (max 60 characters)', 0) end
  local copies = {}
  for _, p in ipairs(photos) do
    selectOnly(p)
    local made
    writeAccess('MHPhotos: virtual copy', function()
      made = catalog():createVirtualCopies(copyName)
    end)
    if type(made) ~= 'table' or not made[1] then
      error('Lightroom created no virtual copy (is the Library module in grid view?)', 0)
    end
    local master = made[1]:getRawMetadata('masterPhoto')
    if master and master:getRawMetadata('uuid') ~= p:getRawMetadata('uuid') then
      error('Lightroom made a virtual copy of ANOTHER photo (copy ' .. tostring(made[1]:getRawMetadata('uuid'))
        .. ', master ' .. tostring(master:getRawMetadata('uuid')) .. '). Delete that copy in Lightroom.', 0)
    end
    copies[#copies + 1] = { source_id = p:getRawMetadata('uuid'), id = made[1]:getRawMetadata('uuid') }
  end
  return { copies = copies }
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
  if name == 'run' or type(fn) ~= 'function' then error('unknown command: ' .. tostring(name), 0) end
  return fn(args or {})
end

return Commands
