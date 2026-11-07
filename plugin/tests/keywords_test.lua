-- keyword_tree and merge_keywords (plug-in 0.1.26) against a small fake keyword tree.
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

local nextId = 10
local function keyword(name, children, synonyms)
  nextId = nextId + 1
  local kw = { localIdentifier = nextId, name = name, children = children or {}, synonyms = synonyms or {}, photos = {} }
  function kw:getName() return self.name end
  function kw:getChildren() return self.children end
  function kw:getSynonyms() return self.synonyms end
  function kw:getPhotos() local out = {}; for p in pairs(self.photos) do out[#out + 1] = p end; return out end
  return kw
end

local function photo(name)
  local p = { name = name, kws = {} }
  function p:addKeyword(kw) self.kws[kw] = true; kw.photos[self] = true end
  function p:removeKeyword(kw) self.kws[kw] = nil; kw.photos[self] = nil end
  return p
end

local barcelona = keyword('Barcelona')
local spanien = keyword('Spanien', { barcelona })
local orte = keyword('Orte', { spanien })
local looseBarcelona = keyword('barcelona ')
local unused = keyword('Leer', nil, { 'empty' })
local top = { orte, looseBarcelona, unused }

local photos = {}
for i = 1, 5 do photos[i] = photo('IMG_' .. i) end
photos[1]:addKeyword(barcelona)
for i = 2, 5 do photos[i]:addKeyword(looseBarcelona) end
photos[2]:addKeyword(barcelona) -- already has the target

local writes = 0
local catalogProxy = {
  getKeywords = function() return top end,
  withWriteAccessDo = function(_, _, fn) writes = writes + 1; fn(); return 'executed' end,
}

_PLUGIN = { id = 'plugin' }
function import(name)
  if name == 'LrTasks' then return { sleep = function() end, pcall = pcall, startAsyncTask = function(fn) fn() end } end
  if name == 'LrDate' then return { currentTime = function() return 0 end, timeToUserFormat = function() return '' end } end
  if name == 'LrApplication' then return { activeCatalog = function() return catalogProxy end } end
  return setmetatable({}, { __index = function() return function() end end })
end

local Commands = require 'Commands'

local t = Commands.run('keyword_tree', { counts = true })
eq(t.total, 5); eq(#t.keywords, 5); eq(t.next_offset, 5)
eq(t.keywords[1].path, 'Orte'); eq(t.keywords[1].children, 1); eq(t.keywords[1].depth, 0)
eq(t.keywords[3].path, 'Orte > Spanien > Barcelona'); eq(t.keywords[3].parent, tostring(spanien.localIdentifier))
eq(t.keywords[3].photos, 2); eq(t.keywords[4].photos, 4); eq(t.keywords[5].photos, 0)
eq(t.keywords[5].synonyms[1], 'empty'); eq(t.keywords[1].synonyms, nil)
local paged = Commands.run('keyword_tree', { offset = 3, limit = 1 })
eq(#paged.keywords, 1); eq(paged.next_offset, 4); eq(paged.keywords[1].photos, nil, 'counts only on request')

-- merge in two calls of at most 2 photos: every photo ends on the target, none on the source
local src, dst = tostring(looseBarcelona.localIdentifier), tostring(barcelona.localIdentifier)
local m1 = Commands.run('merge_keywords', { source_ids = { src }, target_id = dst, max_photos = 2 })
eq(m1.moved, 2); eq(m1.remaining, 2); eq(m1.target, 'Orte > Spanien > Barcelona'); eq(writes, 1)
local m2 = Commands.run('merge_keywords', { source_ids = { src }, target_id = dst })
eq(m2.moved, 2); eq(m2.remaining, 0)
for i = 1, 5 do eq(photos[i].kws[barcelona], true, 'photo ' .. i .. ' has the target'); eq(photos[i].kws[looseBarcelona], nil) end
local m3 = Commands.run('merge_keywords', { source_ids = { src }, target_id = dst })
eq(m3.moved, 0); eq(writes, 2, 'nothing left: no write access asked')

eq(pcall(Commands.run, 'merge_keywords', { source_ids = { dst }, target_id = dst }), false, 'target is not a source')
local okN, errN = pcall(Commands.run, 'merge_keywords', { source_ids = { '9999' }, target_id = dst })
eq(okN, false); eq(errN.code, 'not_found')
eq(pcall(Commands.run, 'merge_keywords', { source_ids = {}, target_id = dst }), false)

print('keywords_test ok')
