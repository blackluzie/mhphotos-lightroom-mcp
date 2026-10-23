-- Tests the auto-export logic against fake Lightroom commands (no Lightroom API involved).
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path
local AutoExport = require 'AutoExport'

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

-- ---- config: off unless explicitly enabled, safe defaults, clamping
eq(AutoExport.normalize(nil, '/h'), nil)
eq(AutoExport.normalize({ collection = 'K' }, '/h'), nil)
eq(AutoExport.normalize({ enabled = 'yes', collection = 'K' }, '/h'), nil)
local c, why = AutoExport.normalize({ enabled = true }, '/h')
eq(c, nil); assert(why:find('collection'), why)
c, why = AutoExport.normalize({ enabled = true, collection = 'K', dest_dir = 'rel/path' }, '/h')
eq(c, nil); assert(why:find('absolute'), why)
c = AutoExport.normalize({ enabled = true, collection = 'Kreuzfahrt' }, '/Users/m')
eq(c.dry_run, true, 'dry run is the default'); eq(c.min_rating, 4); eq(c.max_per_run, 5); eq(c.interval_minutes, 15)
eq(c.dest_dir, '/Users/m/Pictures/MHPhotos-Inbox')
c = AutoExport.normalize({ enabled = true, collection = 'K', dry_run = 'no' }, '/h')
eq(c.dry_run, true, 'only an explicit false ends the dry run')
c = AutoExport.normalize({ enabled = true, collection = 'K', dry_run = false, min_rating = 9, max_per_run = 500,
  interval_minutes = 1, dest_dir = '/Volumes/x' }, '/h')
eq(c.dry_run, false); eq(c.min_rating, 5); eq(c.max_per_run, 20); eq(c.interval_minutes, 5); eq(c.dest_dir, '/Volumes/x')

-- ---- run_once
local all = {}
for i = 1, 8 do all[i] = { id = 'u' .. i, filename = 'IMG_' .. i .. '.dng', rating = 5 } end
local exported, listed = {}, {}
local deps = {
  list_collections = function() return { collections = {
    { id = '1', name = 'Reise' }, { id = '2', name = 'Reisen/Kreuzfahrt' } } } end,
  list_photos = function(args)
    listed[#listed + 1] = args
    local out = {}
    for i = args.offset + 1, math.min(args.offset + args.limit, #all) do out[#out + 1] = all[i] end
    return { total = #all, photos = out }
  end,
  export_photos = function(args) exported[#exported + 1] = args; return { exported = {}, destination = args.dest_dir } end,
}
local cfg = AutoExport.normalize({ enabled = true, collection = 'Kreuzfahrt', max_per_run = 3, dry_run = false }, '/h')
local done = {}

local r = AutoExport.run_once(cfg, done, deps)
eq(r.status, 'exported'); eq(r.exported, 3)
eq(#exported, 1); eq(#exported[1].photo_ids, 3); eq(exported[1].photo_ids[1], 'u1')
eq(exported[1].dest_dir, '/h/Pictures/MHPhotos-Inbox')
eq(listed[1].collection_id, '2', 'matches the last path segment of a nested collection')
eq(listed[1].filters.min_rating, 4)
assert(done.u1 and done.u2 and done.u3 and not done.u4)

-- next run continues with the not yet exported photos, never repeats one
r = AutoExport.run_once(cfg, done, deps)
eq(exported[2].photo_ids[1], 'u4'); eq(#exported[2].photo_ids, 3)
r = AutoExport.run_once(cfg, done, deps)
eq(#exported[3].photo_ids, 2); eq(exported[3].photo_ids[2], 'u8')
r = AutoExport.run_once(cfg, done, deps)
eq(r.status, 'idle'); eq(#exported, 3, 'nothing left, nothing exported')

-- dry run exports nothing and remembers nothing
done = {}
exported = {}
local dry = AutoExport.normalize({ enabled = true, collection = 'Kreuzfahrt' }, '/h')
r = AutoExport.run_once(dry, done, deps)
eq(r.status, 'dry-run'); eq(#r.would_export, 5); eq(#exported, 0); eq(next(done), nil)

-- unknown collection is an error, not a crash
local bad = AutoExport.normalize({ enabled = true, collection = 'Gibt es nicht' }, '/h')
r = AutoExport.run_once(bad, done, deps)
eq(r.status, 'error'); assert(r.error:find('not found'), r.error)

-- paging: already exported photos on the first page do not hide new ones on the next
local many = {}
for i = 1, 130 do many[i] = { id = 'm' .. i, filename = 'M' .. i .. '.dng', rating = 5 } end
all = many
done = {}
for i = 1, 110 do done['m' .. i] = true end
local pc = AutoExport.normalize({ enabled = true, collection = 'Kreuzfahrt', max_per_run = 5, dry_run = false }, '/h')
exported = {}
r = AutoExport.run_once(pc, done, deps)
eq(r.exported, 5); eq(exported[1].photo_ids[1], 'm111')

-- done list round trip, tolerant of garbage
local back = AutoExport.decode_done(AutoExport.encode_done({ a = true, b = true }))
eq(back.a, true); eq(back.b, true)
eq(next(AutoExport.decode_done('not json')), nil); eq(next(AutoExport.decode_done(nil)), nil)
eq(next(AutoExport.decode_done(AutoExport.encode_done({}))), nil)

print('autoexport ok')
