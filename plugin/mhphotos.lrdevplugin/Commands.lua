-- Lightroom-side command implementations. Every function receives the decoded
-- args table and returns a plain table (JSON-encodable) or raises error().
-- Errors with {code='not_found'} are reported to the MCP side as not_found.

local LrApplication = import 'LrApplication'

local Commands = {}

local COCOA_EPOCH_OFFSET = 978307200 -- 2001-01-01 -> 1970-01-01 (seconds)
local MAX_PAGE = 100

local function notFound(msg) error({ code = 'not_found', message = msg }, 0) end

local function catalog() return LrApplication.activeCatalog() end

-- ---------------------------------------------------------------- helpers
local function pickStatusToFlag(p)
  if p == 1 then return 'picked' end
  if p == -1 then return 'rejected' end
  return 'none'
end

local function isoTime(raw)
  if type(raw) ~= 'number' then return '' end
  return os.date('!%Y-%m-%dT%H:%M:%S', raw + COCOA_EPOCH_OFFSET)
end

local function photoSummary(photo)
  return {
    id = photo:getRawMetadata('uuid'),
    filename = photo:getFormattedMetadata('fileName') or '',
    rating = photo:getRawMetadata('rating') or 0,
  }
end

local function photoMetadata(photo)
  local s = photoSummary(photo)
  local keywords = {}
  for _, kw in ipairs(photo:getRawMetadata('keywords') or {}) do
    keywords[#keywords + 1] = kw:getName()
  end
  s.flag = pickStatusToFlag(photo:getRawMetadata('pickStatus'))
  s.keywords = keywords
  s.capture_time = isoTime(photo:getRawMetadata('dateTimeOriginal'))
  s.camera = photo:getFormattedMetadata('cameraModel') or ''
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

local function writeAccess(name, fn)
  local result = catalog():withWriteAccessDo(name, fn, { timeout = 15 })
  if result ~= 'executed' then
    error('write_blocked: Lightroom did not grant catalog write access (' .. tostring(result) .. ')', 0)
  end
end

-- ---------------------------------------------------------------- reads
local function walkCollections(parent, prefix, out)
  for _, c in ipairs(parent:getChildCollections() or {}) do
    out[#out + 1] = {
      id = tostring(c.localIdentifier),
      name = prefix .. c:getName(),
      photo_count = #(c:getPhotos() or {}),
    }
  end
  for _, set in ipairs(parent:getChildCollectionSets() or {}) do
    walkCollections(set, prefix .. set:getName() .. '/', out)
  end
end

function Commands.list_collections()
  local out = {}
  walkCollections(catalog(), '', out)
  return { collections = out }
end

-- Filter criteria (all optional, all must match): min_rating, captured_from, captured_to
-- (YYYY-MM-DD, inclusive), keyword (exact, case-insensitive), flag.
local function matchesFilters(photo, f)
  if f.min_rating and (photo:getRawMetadata('rating') or 0) < f.min_rating then return false end
  if f.captured_from or f.captured_to then
    local day = isoTime(photo:getRawMetadata('dateTimeOriginal')):sub(1, 10)
    if day == '' then return false end
    if f.captured_from and day < f.captured_from then return false end
    if f.captured_to and day > f.captured_to then return false end
  end
  if f.flag and pickStatusToFlag(photo:getRawMetadata('pickStatus')) ~= f.flag then return false end
  if f.keyword then
    local want, found = f.keyword:lower(), false
    for _, kw in ipairs(photo:getRawMetadata('keywords') or {}) do
      if kw:getName():lower() == want then found = true; break end
    end
    if not found then return false end
  end
  return true
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
    source = coll:getPhotos()
  else
    source = catalog():getAllPhotos()
  end
  if filters then
    -- Filtering walks every photo once (fine for thousands, slow for 100k+).
    local kept = {}
    for _, p in ipairs(source) do
      if matchesFilters(p, filters) then kept[#kept + 1] = p end
    end
    source = kept
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

function Commands.add_keywords(args)
  local names = args.keywords or {}
  if #names == 0 or #names > 20 then error('1-20 keywords required', 0) end
  local photos = findAll(args.photo_ids)
  writeAccess('MHPhotos: add keywords', function()
    local cat = catalog()
    for _, name in ipairs(names) do
      local kw = cat:createKeyword(name, {}, true, nil, true)
      for _, p in ipairs(photos) do p:addKeyword(kw) end
    end
  end)
  return { updated = #photos }
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

function Commands.run(name, args)
  local fn = Commands[name]
  if name == 'run' or type(fn) ~= 'function' then error('unknown command: ' .. tostring(name), 0) end
  return fn(args or {})
end

return Commands
