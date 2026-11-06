-- Where and under which name the publish service delivers a rendered photo.
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path
local PublishLogic = require 'PublishLogic'

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

eq(PublishLogic.destDir(nil, '/Users/m'), '/Users/m/Pictures/MHPhotos-Inbox')
eq(PublishLogic.destDir({ dest_dir = '/Volumes/X/in' }, '/Users/m'), '/Volumes/X/in')
eq(PublishLogic.destDir({ dest_dir = '~/Fotos/in' }, '/Users/m'), '/Users/m/Fotos/in')
eq(PublishLogic.destDir({ dest_dir = 'relativ' }, '/Users/m'), '/Users/m/Pictures/MHPhotos-Inbox')
eq(PublishLogic.destDir({ dest_dir = '' }, '/Users/m'), '/Users/m/Pictures/MHPhotos-Inbox')

eq(PublishLogic.targetName('IMG_0329.DNG', 'AB-CD'), 'IMG_0329.jpg')
eq(PublishLogic.targetName('Mein Foto (1).heic', 'u'), 'Mein_Foto__1_.jpg')
eq(PublishLogic.targetName(nil, 'u'), 'photo.jpg')
eq(PublishLogic.targetName('IMG_1.DNG', '1A2B3C4D-9999', function(n) return n == 'IMG_1.jpg' end), 'IMG_1-1A2B3C4D.jpg')
eq(PublishLogic.targetName('IMG_1.DNG', 'x', function() return false end), 'IMG_1.jpg')

local files, removed, taken = {}, {}, { ['/in/IMG_2.jpg'] = true }
local deps = {
  copy = function(from, to) if from == '/tmp/bad' then return false, 'disk full' end files[to] = from; return true end,
  exists = function(p) return taken[p] or false end,
  remove = function(p) removed[#removed + 1] = p end,
}
local ok, name = PublishLogic.deliver('/tmp/r1.jpg', 'IMG_1.DNG', 'u1', '/in', deps)
eq(ok, true); eq(name, 'IMG_1.jpg'); eq(files['/in/IMG_1.jpg'], '/tmp/r1.jpg'); eq(removed[1], '/tmp/r1.jpg')
ok, name = PublishLogic.deliver('/tmp/r2.jpg', 'IMG_2.DNG', 'abcdef123456', '/in', deps)
eq(ok, true); eq(name, 'IMG_2-abcdef12.jpg')
ok, name = PublishLogic.deliver('/tmp/bad', 'IMG_3.DNG', 'u3', '/in', deps)
eq(ok, false); eq(name, 'disk full'); eq(removed[3], '/tmp/bad', 'the temporary render is removed even when the copy fails')
print('publish ok')

-- the provider file itself: loads, and processRenderedPhotos hands each rendition to the hot folder
local copied, recorded, failed, made = {}, {}, {}, {}
local FS = {
  readFile = function(path) if path:find('autoexport.json', 1, true) then return '{"dest_dir": "/hot"}' end end,
  createAllDirectories = function(d) made[#made + 1] = d end,
  copy = function(from, to) copied[#copied + 1] = from .. '>' .. to; return true end,
  exists = function() return false end,
  delete = function() end,
}
function import(name)
  if name == 'LrView' then return {} end
  if name == 'LrFileUtils' then return FS end
  if name == 'LrPathUtils' then return { getStandardFilePath = function() return '/home/u' end, child = function(a, b) return a .. '/' .. b end } end
  if name == 'LrTasks' then return { yield = function() end } end
  error('unexpected import ' .. name)
end
local provider = dofile('../mhphotos.lrdevplugin/PublishServiceProvider.lua')
eq(provider.supportsIncrementalPublish, 'only')
eq(provider.canExportVideo, false)
local function rendition(name, uuid, ok, path)
  return {
    photo = { getFormattedMetadata = function() return name end, getRawMetadata = function() return uuid end },
    waitForRender = function() return ok, path end,
    recordPublishedPhotoId = function(_, id) recorded[#recorded + 1] = id end,
    uploadFailed = function(_, msg) failed[#failed + 1] = msg end,
  }
end
local list = { rendition('IMG_1.DNG', 'u1', true, '/tmp/a.jpg'), rendition('IMG_2.DNG', 'u2', false, 'render error') }
local ctx = {
  exportSession = { countRenditions = function() return #list end },
  configureProgress = function() return { isCanceled = function() return false end } end,
  renditions = function()
    local i = 0
    return function() i = i + 1; if list[i] then return i, list[i] end end
  end,
}
provider.processRenderedPhotos({}, ctx)
eq(made[1], '/hot'); eq(copied[1], '/tmp/a.jpg>/hot/IMG_1.jpg'); eq(recorded[1], 'IMG_1.jpg')
eq(failed[1], 'render error'); eq(#recorded, 1)
local deleted = {}
provider.deletePhotosFromPublishedCollection({}, { 'a', 'b' }, function(id) deleted[#deleted + 1] = id end)
eq(table.concat(deleted, ','), 'a,b')
local sections = provider.sectionsForTopOfDialog({ row = function(_, t) return t end, static_text = function(t) return t end }, {})
eq(#sections, 1); eq(sections[1].title, 'MHPhotos')
print('publish provider ok')
