-- "MHPhotos" publish service: put photos into a published collection, Lightroom renders them as JPEG and
-- this hands them to the hot folder (see PublishLogic.lua). "Publish" means: sent to the MHPhotos Foto-Inbox
-- for review; nothing goes online or to Instagram from here.
--
-- If Lightroom ever has trouble with this service, remove the LrExportServiceProvider line in Info.lua.

local LrView = import 'LrView'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrTasks = import 'LrTasks'

local Json = require 'Json'
local PublishLogic = require 'PublishLogic'

local function readConfig(home)
  local raw = LrFileUtils.readFile(LrPathUtils.child(LrPathUtils.child(home, '.mhphotos-lightroom-mcp'), 'autoexport.json'))
  if not raw then return nil end
  local ok, cfg = pcall(Json.decode, raw)
  return ok and cfg or nil
end

local provider = {}

provider.supportsIncrementalPublish = 'only'
provider.canExportVideo = false
provider.allowFileFormats = { 'JPEG' }
provider.allowColorSpaces = { 'sRGB' }
provider.hidePrintResolution = true
provider.hideSections = { 'exportLocation', 'fileNaming', 'video' }
provider.exportPresetFields = {}

function provider.sectionsForTopOfDialog(f, _)
  return {
    {
      title = 'MHPhotos',
      f:row {
        f:static_text {
          title = 'Fotos in dieser Sammlung werden als JPEG in die MHPhotos-Foto-Inbox gelegt (Status „wartet“).\n'
            .. 'Veröffentlicht wird erst, wenn du sie in MHPhotos freigibst.',
          width_in_chars = 60, height_in_lines = 2,
        },
      },
    },
  }
end

function provider.processRenderedPhotos(functionContext, exportContext)
  local home = LrPathUtils.getStandardFilePath('home')
  local dir = PublishLogic.destDir(readConfig(home), home)
  LrFileUtils.createAllDirectories(dir)
  local progress = exportContext:configureProgress({
    title = 'MHPhotos: Fotos in die Inbox legen (' .. exportContext.exportSession:countRenditions() .. ')',
  })
  local deps = {
    copy = function(from, to) return LrFileUtils.copy(from, to) end,
    exists = function(path) return LrFileUtils.exists(path) ~= false end,
    remove = function(path) LrFileUtils.delete(path) end,
  }
  for _, rendition in exportContext:renditions({ stopIfCanceled = true }) do
    local ok, pathOrMessage = rendition:waitForRender()
    if progress:isCanceled() then break end
    if ok then
      local photo = rendition.photo
      local delivered, nameOrErr = PublishLogic.deliver(
        pathOrMessage, photo:getFormattedMetadata('fileName'), photo:getRawMetadata('uuid'), dir, deps)
      if delivered then
        rendition:recordPublishedPhotoId(nameOrErr)
      else
        rendition:uploadFailed(nameOrErr)
      end
    else
      rendition:uploadFailed(tostring(pathOrMessage))
    end
    LrTasks.yield()
  end
end

-- Removing a photo from the published collection only clears Lightroom's mark; nothing is deleted in MHPhotos.
function provider.deletePhotosFromPublishedCollection(_, arrayOfPhotoIds, deletedCallback)
  for _, id in ipairs(arrayOfPhotoIds) do deletedCallback(id) end
end

return provider
