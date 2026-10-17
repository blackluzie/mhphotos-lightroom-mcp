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
  function p:setRawMetadata(k, v)
    if k == 'title' or k == 'caption' then self.fmt[k] = v else self.raw[k] = v end
  end
  function p:addKeyword(kw) self.raw.keywords[#self.raw.keywords + 1] = kw end
  function p:removeKeyword(kw)
    for i, k in ipairs(self.raw.keywords) do if k == kw then table.remove(self.raw.keywords, i); return end end
  end
  p.develop = { Exposure2012 = 0.5, Contrast2012 = 10, ToneCurve = { 1, 2 }, CameraProfile = 'Adobe Color' }
  p.appliedPresets, p.appliedSettings = {}, {}
  function p:getDevelopSettings() return self.develop end
  function p:applyDevelopPreset(preset, plugin) self.appliedPresets[#self.appliedPresets + 1] = { preset, plugin } end
  function p:applyDevelopSettings(settings, name) self.appliedSettings[#self.appliedSettings + 1] = { settings, name } end
  function p:requestJpegThumbnail(w, h, cb) cb(self.fmt.fileName .. ':' .. w .. 'x' .. h, nil); return {} end
  return p
end

local p1 = newPhoto('u1', 'IMG_1.dng', 5, 1, { 'a', 'b' }, 0, 'Canon')
local p2 = newPhoto('u2', 'IMG_2.dng', nil, 0, {}, 86400, 'Nikon')
-- p1 was taken in South Tyrol, p2 has no place data
p1.raw.gps = { latitude = 46.6, longitude = 11.5 }
p1.raw.isoSpeedRating = 200; p1.raw.aperture = 8; p1.raw.focalLength = 35
p1.fmt.city = 'Brixen'; p1.fmt.stateProvince = 'Südtirol'; p1.fmt.country = 'Italien'
p1.fmt.shutterSpeed = '1/250'; p1.fmt.lens = '24-70'
function p2:getRawMetadata(k) return self.raw[k] end
local photos = { p1, p2 }

local coll = { localIdentifier = 7, getName = function() return 'Fav' end, getPhotos = function() return { p1 } end }
local set = { getName = function() return 'Set' end,
  getChildCollections = function() return { { localIdentifier = 8, getName = function() return 'Inner' end,
    getPhotos = function() return { p2, p1 } end } } end,
  getChildCollectionSets = function() return {} end }

local writes = 0
local lastWriteOpts
local createdCollection, selected, activeSource, virtualCopyName
local viewCalls = {}
local fakeCatalog = {
  getChildCollections = function() return { coll } end,
  getChildCollectionSets = function() return { set } end,
  getAllPhotos = function() return photos end,
  findPhotoByUuid = function(_, id) for _, p in ipairs(photos) do if p.raw.uuid == id then return p end end end,
  getCollectionByLocalIdentifier = function(_, id) if id == 7 then return coll end end,
  withWriteAccessDo = function(_, name, fn, opts) writes = writes + 1; lastWriteOpts = opts; fn(); return 'executed' end,
  createKeyword = function(_, name) return { getName = function() return name end } end,
  createCollection = function(_, name, parent, prior) createdCollection = { name = name, prior = prior, localIdentifier = 99,
    addPhotos = function(self, ps) self.added = ps end, isSmartCollection = function() return false end }; return createdCollection end,
  setSelectedPhotos = function(_, p) selected = p end,
  setActiveSources = function(_, src) activeSource = src; return true end,
  createVirtualCopies = function(_, copyName)
    virtualCopyName = copyName
    local copy = newPhoto('copy-of-' .. selected.raw.uuid, 'IMG_copy.dng', 0, 0, {}, 0, 'x')
    copy.raw.isVirtualCopy = true; copy.raw.masterPhoto = selected
    photos[#photos + 1] = copy
    return { copy }
  end,
}
-- methods are called with ':' -> need self first
local catalogProxy = setmetatable({ kAllPhotos = 'kAllPhotos' }, { __index = function(_, k)
  local f = fakeCatalog[k]
  if f == nil then return nil end
  return function(self, ...) return f(self, ...) end end })

local exported
local createdPreset
local presetA = { getName = function() return 'Warm' end }
local presetB = { getName = function() return 'Warm' end }
local presetC = { getName = function() return 'Klar' end }
local folders = {
  { getName = function() return 'User Presets' end, getDevelopPresets = function() return { presetA, presetC } end },
  { getName = function() return 'Adobe' end, getDevelopPresets = function() return { presetB } end },
}
_PLUGIN = { id = 'plugin' }
function import(name)
  if name == 'LrApplicationView' then
    return { switchToModule = function(m) viewCalls[#viewCalls + 1] = 'module:' .. m end,
             gridView = function() viewCalls[#viewCalls + 1] = 'grid' end }
  end
  if name == 'LrTasks' then return { sleep = function() end, pcall = pcall } end
  if name == 'LrDate' then return { currentTime = function() return os.clock() end } end
  if name == 'LrStringUtils' then return { encodeBase64 = function(d) return 'B64(' .. d .. ')' end } end
  if name == 'LrApplication' then
    return {
      activeCatalog = function() return catalogProxy end,
      versionString = function() return '15.0' end,
      developPresetFolders = function() return folders end,
      addDevelopPresetForPlugin = function(plugin, title, settings) createdPreset = { plugin, title, settings }; return { handle = title } end,
    }
  end
  if name == 'LrExportSession' then
    return function(spec) exported = spec; return { doExportOnCurrentTask = function() end } end
  end
  error('unexpected import ' .. name)
end

local Commands = require 'Commands'

-- reads
local cols = Commands.run('list_collections').collections
eq(#cols, 2); eq(cols[1].name, 'Fav'); eq(cols[2].name, 'Set/Inner'); eq(cols[2].id, '8')
eq(cols[1].photo_count, nil)  -- counts are only computed on request (slow on big catalogs)
local counted = Commands.run('list_collections', { include_counts = true }).collections
eq(counted[1].photo_count, 1); eq(counted[2].photo_count, 2)

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

-- place / GPS filters and extra metadata
eq(ids(Commands.run('list_photos', { filters = { location = 'südtirol' } })), 'u1')
eq(ids(Commands.run('list_photos', { filters = { near_lat = 46.7, near_lon = 11.4, radius_km = 30 } })), 'u1')
eq(Commands.run('list_photos', { filters = { near_lat = 51.9, near_lon = 7.6, radius_km = 30 } }).total, 0)
local meta = Commands.run('get_photo', { photo_id = 'u1' })
eq(meta.location, 'Brixen, Südtirol, Italien'); eq(meta.gps.latitude, 46.6); eq(meta.iso, 200)
eq(meta.shutter_speed, '1/250'); eq(meta.focal_length_mm, 35)
eq(Commands.run('get_photo', { photo_id = 'u2' }).gps, nil)

-- preview
eq(Commands.run('get_preview', { photo_id = 'u1', size = 99999 }).image_base64, 'B64(IMG_1.dng:1280x1280)')
eq(Commands.run('get_preview', { photo_id = 'u1', size = 1 }).image_base64, 'B64(IMG_1.dng:256x256)')
eq(select(2, pcall(Commands.run, 'get_preview', { photo_id = 'zzz' })).code, 'not_found')

-- develop settings: scalars only (the ToneCurve table is dropped)
local ds = Commands.run('get_develop_settings', { photo_id = 'u1' }).settings
eq(ds.Exposure2012, 0.5); eq(ds.CameraProfile, 'Adobe Color'); eq(ds.ToneCurve, nil)

-- presets
local pl = Commands.run('list_presets').presets
eq(#pl, 3); eq(pl[1].folder, 'User Presets'); eq(pl[3].folder, 'Adobe')
Commands.run('apply_preset', { photo_ids = { 'u1' }, preset = 'Klar' })
eq(p1.appliedPresets[1][1], presetC); eq(p1.appliedPresets[1][2], _PLUGIN)
eq(select(2, pcall(Commands.run, 'apply_preset', { photo_ids = { 'u1' }, preset = 'Warm' })).code, 'not_found')  -- ambiguous
Commands.run('apply_preset', { photo_ids = { 'u1' }, preset = 'Adobe/Warm' })
eq(p1.appliedPresets[2][1], presetB)
local wb = writes
eq(select(2, pcall(Commands.run, 'apply_preset', { photo_ids = { 'u1' }, preset = 'Nope' })).code, 'not_found')
eq(writes, wb)  -- unknown preset: no write access requested

-- develop settings + create preset
Commands.run('apply_develop_settings', { photo_ids = { 'u1', 'u2' }, settings = { Exposure2012 = 0.3, ConvertToGrayscale = false } })
eq(p2.appliedSettings[1][1].Exposure2012, 0.3)
eq(pcall(Commands.run, 'apply_develop_settings', { photo_ids = { 'u1' }, settings = {} }), false)
eq(pcall(Commands.run, 'apply_develop_settings', { photo_ids = { 'u1' }, settings = { Exposure2012 = 'x' } }), false)
local created = Commands.run('create_preset', { name = 'Mein Preset', settings = { Contrast2012 = 20 } })
eq(created.listed_in_presets, false); eq(created.returned_by_lightroom, true)
-- not listed by Lightroom, but apply_preset still finds it through the kept handle
local before = #p1.appliedPresets
Commands.run('apply_preset', { photo_ids = { 'u1' }, preset = 'Mein Preset' })
eq(#p1.appliedPresets, before + 1); eq(p1.appliedPresets[before + 1][1].handle, 'Mein Preset')
eq(createdPreset[2], 'Mein Preset'); eq(createdPreset[3].Contrast2012, 20); eq(createdPreset[1], _PLUGIN)
eq(pcall(Commands.run, 'create_preset', { name = '', settings = { Contrast2012 = 20 } }), false)

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


-- filter cache: the filter is evaluated once per photo, paging reuses it, a write clears it
local ratingReads = 0
for _, p in ipairs({ p1, p2 }) do
  local orig = p.getRawMetadata
  p.getRawMetadata = function(self, k) if k == 'rating' then ratingReads = ratingReads + 1 end return orig(self, k) end
end
local function reads(fn) local before = ratingReads; fn(); return ratingReads - before end
local listOnce = function() Commands.run('list_photos', { filters = { min_rating = 1 }, limit = 1 }) end
local first = reads(listOnce)   -- scans both photos, then lists one
local second = reads(listOnce)  -- served from the cache: only the listed row is read
assert(first >= 3 and second == 1, 'cache miss ' .. first .. ' hit ' .. second)
Commands.run('set_flag', { photo_ids = { 'u2' }, flag = 'none' })  -- any write clears the cache
local third = reads(listOnce)
assert(third >= 3, 'cache must be rebuilt after a write, reads=' .. third)


-- organising: collection, label, title/caption, keyword removal, virtual copy
local made = Commands.run('create_collection', { name = 'Südtirol 09 2026' })
eq(made.id, '99'); eq(createdCollection.name, 'Südtirol 09 2026'); eq(createdCollection.prior, true)
eq(pcall(Commands.run, 'create_collection', { name = '' }), false)
coll.addPhotos = function(self, ps) self.added = ps end
coll.isSmartCollection = function() return false end
Commands.run('add_to_collection', { collection_id = '7', photo_ids = { 'u1', 'u2' } })
eq(#coll.added, 2)
coll.isSmartCollection = function() return true end
eq(pcall(Commands.run, 'add_to_collection', { collection_id = '7', photo_ids = { 'u1' } }), false)
eq(select(2, pcall(Commands.run, 'add_to_collection', { collection_id = '55', photo_ids = { 'u1' } })).code, 'not_found')
Commands.run('set_color_label', { photo_ids = { 'u1' }, label = 'green' })
eq(p1.raw.colorNameForLabel, 'green')
eq(pcall(Commands.run, 'set_color_label', { photo_ids = { 'u1' }, label = 'pink' }), false)
Commands.run('set_title_caption', { photo_ids = { 'u1' }, title = 'Seiser Alm' })
eq(p1.fmt.title, 'Seiser Alm'); eq(p1.fmt.caption, nil)
Commands.run('set_title_caption', { photo_ids = { 'u1' }, caption = '' })
eq(p1.fmt.caption, '')
eq(pcall(Commands.run, 'set_title_caption', { photo_ids = { 'u1' } }), false)
local m1 = Commands.run('get_photo', { photo_id = 'u1' })
eq(m1.title, 'Seiser Alm'); eq(m1.color_label, 'green')
p1.raw.colorNameForLabel = 'grey'  -- Lightroom's wording for "no colour"
eq(Commands.run('get_photo', { photo_id = 'u1' }).color_label, 'none')
p1.raw.colorNameForLabel = 'green'
Commands.run('add_keywords', { photo_ids = { 'u1' }, keywords = { 'zz-added' } })
Commands.run('remove_keywords', { photo_ids = { 'u1' }, keywords = { 'ZZ-ADDED' } })
for _, kw in ipairs(p1.raw.keywords) do assert(kw:getName() ~= 'zz-added', 'keyword should be gone') end
eq(#p1.raw.keywords >= 2, true)  -- the original keywords 'a','b' stay
local vc = Commands.run('create_virtual_copies', { photo_ids = { 'u1' } }).copies
eq(vc[1].source_id, 'u1'); eq(vc[1].id, 'copy-of-u1')
eq(Commands.run('get_photo', { photo_id = 'copy-of-u1' }).virtual_copy_of, 'u1')
eq(pcall(Commands.run, 'create_virtual_copies', { photo_ids = {} }), false)
-- the copy is made from the Library grid, with 'All Photographs' as source and only that photo selected
eq(table.concat(viewCalls, ','), 'module:library,grid'); eq(activeSource, 'kAllPhotos')
eq(selected.raw.uuid, 'u1'); eq(virtualCopyName, 'MHPhotos')
-- write access never queues: it waits (asynchronous=false) with a timeout
eq(lastWriteOpts.asynchronous, false); eq(lastWriteOpts.timeout, 15)
-- a collection SET id (no getPhotos/addPhotos) gets a clear message, not a Lua crash
local setObj = { localIdentifier = 70, getName = function() return 'S' end }
local origGet = fakeCatalog.getCollectionByLocalIdentifier
fakeCatalog.getCollectionByLocalIdentifier = function(_, id) if id == 70 then return setObj end return origGet(_, id) end
local okA, errA = pcall(Commands.run, 'add_to_collection', { collection_id = '70', photo_ids = { 'u1' } })
eq(okA, false); assert(tostring(errA):find('collection set'), tostring(errA))
local okL, errL = pcall(Commands.run, 'list_photos', { collection_id = '70' })
eq(okL, false); assert(tostring(errL):find('collection set'), tostring(errL))
fakeCatalog.getCollectionByLocalIdentifier = origGet

-- self test: all green on the fake, and a failing API call is reported without stopping the rest
local st = Commands.run('selftest')
eq(st.failed, 0); assert(st.passed >= 12, 'expected at least 12 checks, got ' .. st.passed); eq(st.environment, 'Lightroom 15.0')
local origDev = p1.getDevelopSettings
p1.getDevelopSettings = function() error('getDevelopSettings exploded') end
local st2 = Commands.run('selftest')
eq(st2.failed, 1)
for _, c in ipairs(st2.checks) do
  if not c.ok then eq(c.name, 'get_develop_settings'); assert(c.detail:find('exploded'), c.detail) end
end
p1.getDevelopSettings = origDev
-- a Lightroom without one of the write functions is reported by name, without calling anything
local origVC = fakeCatalog.createVirtualCopies
fakeCatalog.createVirtualCopies = nil
local st3 = Commands.run('selftest')
eq(st3.failed, 1)
for _, c in ipairs(st3.checks) do
  if not c.ok then eq(c.name, 'API present: catalog'); assert(c.detail:find('createVirtualCopies'), c.detail) end
end
fakeCatalog.createVirtualCopies = origVC

print('commands ok')
