-- Tests Commands.lua logic against a fake Lightroom API (names are the ones the plugin
-- uses; they are NOT verified against real Lightroom here).
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

-- ---- fakes
local function newPhoto(uuid, name, rating, pick, kws, t, cam)
  local p = { raw = { uuid = uuid, rating = rating, pickStatus = pick, keywords = {}, dateTimeOriginal = t },
    fmt = { fileName = name, cameraModel = cam } }
  for _, k in ipairs(kws) do p.raw.keywords[#p.raw.keywords + 1] = { getName = function() return k end } end
  function p:getRawMetadata(k) return self.raw[k] end
  function p:getFormattedMetadata(k) return self.fmt[k] end
  function p:setRawMetadata(k, v) self.raw[k] = v end
  function p:addKeyword(kw) self.raw.keywords[#self.raw.keywords + 1] = kw end
  return p
end

local p1 = newPhoto('u1', 'IMG_1.dng', 5, 1, { 'a', 'b' }, 0, 'Canon')
local p2 = newPhoto('u2', 'IMG_2.dng', nil, 0, {}, 86400, 'Nikon')
local photos = { p1, p2 }

local coll = { localIdentifier = 7, getName = function() return 'Fav' end, getPhotos = function() return { p1 } end }
local set = { getName = function() return 'Set' end,
  getChildCollections = function() return { { localIdentifier = 8, getName = function() return 'Inner' end,
    getPhotos = function() return { p2, p1 } end } } end,
  getChildCollectionSets = function() return {} end }

local writes = 0
local fakeCatalog = {
  getChildCollections = function() return { coll } end,
  getChildCollectionSets = function() return { set } end,
  getAllPhotos = function() return photos end,
  findPhotoByUuid = function(_, id) for _, p in ipairs(photos) do if p.raw.uuid == id then return p end end end,
  getCollectionByLocalIdentifier = function(_, id) if id == 7 then return coll end end,
  withWriteAccessDo = function(_, name, fn, opts) writes = writes + 1; fn(); return 'executed' end,
  createKeyword = function(_, name) return { getName = function() return name end } end,
}
-- methods are called with ':' -> need self first
local catalogProxy = setmetatable({}, { __index = function(_, k)
  local f = fakeCatalog[k]; return function(self, ...) return f(self, ...) end end })

local exported
function import(name)
  if name == 'LrApplication' then return { activeCatalog = function() return catalogProxy end } end
  if name == 'LrExportSession' then
    return function(spec) exported = spec; return { doExportOnCurrentTask = function() end } end
  end
  error('unexpected import ' .. name)
end

local Commands = require 'Commands'

-- reads
local cols = Commands.run('list_collections').collections
eq(#cols, 2); eq(cols[1].name, 'Fav'); eq(cols[1].photo_count, 1)
eq(cols[2].name, 'Set/Inner'); eq(cols[2].id, '8'); eq(cols[2].photo_count, 2)

local page = Commands.run('list_photos', { limit = 1, offset = 1 })
eq(page.total, 2); eq(#page.photos, 1); eq(page.photos[1].id, 'u2'); eq(page.photos[1].rating, 0)
eq(Commands.run('list_photos', { collection_id = '7' }).total, 1)
eq(select(2, pcall(Commands.run, 'list_photos', { collection_id = '99' })).code, 'not_found')

local m = Commands.run('get_photo', { photo_id = 'u1' })
eq(m.flag, 'picked'); eq(m.keywords[2], 'b'); eq(m.camera, 'Canon'); eq(m.capture_time, '2001-01-01T00:00:00')
eq(Commands.run('get_photo', { photo_id = 'u2' }).flag, 'none')
eq(select(2, pcall(Commands.run, 'get_photo', { photo_id = 'zzz' })).code, 'not_found')

-- filters (p1: 5 stars, picked, kw a/b, 2001-01-01; p2: unrated, none, 2001-01-02)
local function ids(r) local o = {} for _, x in ipairs(r.photos) do o[#o + 1] = x.id end return table.concat(o, ',') end
eq(ids(Commands.run('list_photos', { filters = { min_rating = 5 } })), 'u1')
eq(ids(Commands.run('list_photos', { filters = { captured_from = '2001-01-02' } })), 'u2')
eq(ids(Commands.run('list_photos', { filters = { captured_to = '2001-01-01' } })), 'u1')
eq(ids(Commands.run('list_photos', { filters = { keyword = 'A' } })), 'u1')
eq(ids(Commands.run('list_photos', { filters = { flag = 'none' } })), 'u2')
local fr = Commands.run('list_photos', { filters = { min_rating = 5, flag = 'none' } })
eq(fr.total, 0)
eq(Commands.run('list_photos', { filters = {} }).total, 2)
eq(Commands.run('list_photos', { filters = { min_rating = 1 }, limit = 1, offset = 0 }).total, 1)

-- writes
Commands.run('set_rating', { photo_ids = { 'u1', 'u2' }, rating = 3 })
eq(p1.raw.rating, 3); eq(p2.raw.rating, 3)
Commands.run('set_rating', { photo_ids = { 'u1' }, rating = 0 })
eq(p1.raw.rating, nil)
Commands.run('set_flag', { photo_ids = { 'u2' }, flag = 'rejected' })
eq(p2.raw.pickStatus, -1)
Commands.run('add_keywords', { photo_ids = { 'u2' }, keywords = { 'x' } })
eq(p2.raw.keywords[1]:getName(), 'x')

-- a bad id must abort BEFORE any change (no write access requested at all)
local before = writes
local ok, err = pcall(Commands.run, 'set_rating', { photo_ids = { 'u1', 'nope' }, rating = 1 })
eq(ok, false); eq(err.code, 'not_found'); eq(writes, before); eq(p1.raw.rating, nil)

-- input validation
eq(pcall(Commands.run, 'set_rating', { photo_ids = { 'u1' }, rating = 9 }), false)
eq(pcall(Commands.run, 'set_flag', { photo_ids = { 'u1' }, flag = 'bogus' }), false)
eq(pcall(Commands.run, 'set_rating', { photo_ids = {}, rating = 1 }), false)
eq(pcall(Commands.run, 'run', {}), false)
eq(pcall(Commands.run, 'does_not_exist', {}), false)

-- export
local r = Commands.run('export_photos', { photo_ids = { 'u1' }, dest_dir = '/tmp/out' })
eq(r.exported[1], 'IMG_1.jpg'); eq(exported.exportSettings.LR_export_destinationPathPrefix, '/tmp/out')
eq(exported.exportSettings.LR_format, 'JPEG')
eq(pcall(Commands.run, 'export_photos', { photo_ids = { 'u1' } }), false)

print('commands ok')
