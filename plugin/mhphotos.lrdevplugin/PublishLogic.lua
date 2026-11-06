-- The part of the "MHPhotos" publish service that needs no Lightroom API, so it can be tested:
-- where a rendered photo goes and under which name. The service drops finished JPEGs into the hot
-- folder; the HTTP service then checks them, batches them and n8n uploads them to the MHPhotos Foto-Inbox
-- with status "wartet". Nothing is published without approval in MHPhotos.

local PublishLogic = {}

local DEFAULT_DEST = '/Pictures/MHPhotos-Inbox'

-- autoexport.json may name another folder (dest_dir); otherwise ~/Pictures/MHPhotos-Inbox.
function PublishLogic.destDir(autoexportConfig, home)
  local dest = type(autoexportConfig) == 'table' and autoexportConfig.dest_dir or nil
  if type(dest) == 'string' and dest ~= '' then
    if dest:sub(1, 2) == '~/' then dest = home .. dest:sub(2) end
    if dest:sub(1, 1) == '/' then return dest end
  end
  return home .. DEFAULT_DEST
end

-- 'IMG_0329.DNG' -> 'IMG_0329.jpg'; on a name clash 'IMG_0329-1a2b3c4d.jpg' (uuid start).
function PublishLogic.targetName(fileName, uuid, exists)
  local base = tostring(fileName or 'photo'):gsub('%.[^.]*$', ''):gsub('[^%w%._%-]', '_')
  if base == '' then base = 'photo' end
  local name = base .. '.jpg'
  if exists and exists(name) then
    local tag = tostring(uuid or ''):gsub('[^%w]', ''):sub(1, 8)
    if tag == '' then tag = tostring(os.time()) end
    name = base .. '-' .. tag .. '.jpg'
  end
  return name
end

-- One rendition. deps: copy(from, to) -> ok, err; exists(path); remove(path).
-- Returns ok, nameOrMessage; the temporary render is always removed.
function PublishLogic.deliver(renderedPath, fileName, uuid, dir, deps)
  local name = PublishLogic.targetName(fileName, uuid, function(n) return deps.exists(dir .. '/' .. n) end)
  local ok, err = deps.copy(renderedPath, dir .. '/' .. name)
  deps.remove(renderedPath)
  if not ok then return false, tostring(err or 'copy failed') end
  return true, name
end

return PublishLogic
