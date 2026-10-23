-- Library > Plug-in Extras > MHPhotos MCP: Auto-Export einrichten
-- Writes ~/.mhphotos-lightroom-mcp/autoexport.json; the running auto-export task reads it again
-- every round, so no reload is needed. Only the collection list is read from the catalog.
local LrTasks = import 'LrTasks'
local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'

-- Errors in menu scripts are otherwise swallowed by Lightroom: trace the steps and show failures.
local function trace(text)
  pcall(function()
    local dir = LrPathUtils.child(LrPathUtils.getStandardFilePath('home'), '.mhphotos-lightroom-mcp')
    LrFileUtils.createAllDirectories(dir)
    local f = io.open(LrPathUtils.child(dir, 'autoexport-setup.log'), 'a')
    if f then f:write(os.date('%Y-%m-%d %H:%M:%S ') .. text .. '\n'); f:close() end
  end)
end

local function setup()
  trace('setup: start')
  local LrFunctionContext = import 'LrFunctionContext'
  local LrBinding = import 'LrBinding'
  local LrView = import 'LrView'
  local Commands = require 'Commands'
  local Json = require 'Json'
  local AutoExport = require 'AutoExport'

  local dir = LrPathUtils.child(LrPathUtils.getStandardFilePath('home'), '.mhphotos-lightroom-mcp')
  local path = LrPathUtils.child(dir, 'autoexport.json')
  local existing
  if LrFileUtils.exists(path) then
    local okRead, raw = pcall(Json.decode, LrFileUtils.readFile(path) or '')
    if okRead and type(raw) == 'table' then existing = raw end
  end

  trace('setup: modules loaded')
  trace('setup: reading collections')
  local okList, listed = LrTasks.pcall(Commands.list_collections, {})
  trace('setup: collections read ok=' .. tostring(okList) .. ' type=' .. type(listed))
  if not okList then
    LrDialogs.message('MHPhotos Auto-Export', 'Sammlungen konnten nicht gelesen werden: ' .. tostring(listed), 'critical')
    return
  end
  local values = AutoExport.dialog_values(existing)
  trace('setup: values ready, collection list has ' .. tostring(#listed.collections))
  local items, known = {}, {}
  for _, c in ipairs(listed.collections) do
    items[#items + 1] = { title = c.name, value = c.name }
    known[c.name] = true
  end
  if values.collection ~= '' and not known[values.collection] then
    items[#items + 1] = { title = values.collection .. ' (nicht gefunden)', value = values.collection }
  end
  trace('setup: items built: ' .. #items)
  if #items == 0 then
    LrDialogs.message('MHPhotos Auto-Export', 'Dieser Katalog hat noch keine Sammlung. Lege zuerst eine an.', 'info')
    return
  end
  if values.collection == '' then values.collection = items[1].value end

  trace('setup: ' .. #items .. ' collections, opening dialog')
  LrFunctionContext.callWithContext('mhphotos-autoexport-setup', function(context)
    local f = LrView.osFactory()
    local props = LrBinding.makePropertyTable(context)
    for k, v in pairs(values) do props[k] = v end
    local stars = {}
    for i = 1, 5 do stars[i] = { title = tostring(i), value = i } end
    local contents = f:column {
      bind_to_object = props,
      spacing = f:control_spacing(),
      f:static_text { title = 'Exportiert die besten neuen Fotos einer Sammlung in den MHPhotos-Eingangsordner.' },
      f:row {
        f:static_text { title = 'Sammlung:', width = LrView.share('mhphotos_label') },
        f:popup_menu { value = LrView.bind('collection'), items = items, width_in_chars = 32 },
      },
      f:row {
        f:static_text { title = 'Mindestbewertung (Sterne):', width = LrView.share('mhphotos_label') },
        f:popup_menu { value = LrView.bind('min_rating'), items = stars },
      },
      f:row {
        f:static_text { title = 'Höchstens Fotos pro Runde:', width = LrView.share('mhphotos_label') },
        f:edit_field { value = LrView.bind('max_per_run'), min = 1, max = 20, precision = 0, width_in_digits = 4 },
      },
      f:row {
        f:static_text { title = 'Alle ... Minuten:', width = LrView.share('mhphotos_label') },
        f:edit_field { value = LrView.bind('interval_minutes'), min = 5, max = 1440, precision = 0, width_in_digits = 5 },
      },
      f:checkbox { title = 'Probelauf (nur anzeigen, was exportiert würde)', value = LrView.bind('dry_run') },
      f:checkbox { title = 'Auto-Export aktiv', value = LrView.bind('enabled') },
    }
    local result = LrDialogs.presentModalDialog {
      title = 'MHPhotos: Auto-Export einrichten', contents = contents, actionVerb = 'Speichern',
    }
    trace('setup: dialog closed with ' .. tostring(result))
    if result ~= 'ok' then return end

    local cfg = AutoExport.build_config({
      enabled = props.enabled, collection = props.collection, min_rating = props.min_rating,
      max_per_run = props.max_per_run, interval_minutes = props.interval_minutes, dry_run = props.dry_run,
    }, existing)
    LrFileUtils.createAllDirectories(dir)
    local out = io.open(path, 'w')
    if not out then
      LrDialogs.message('MHPhotos Auto-Export', 'Konnte ' .. path .. ' nicht schreiben.', 'critical')
      return
    end
    out:write(Json.encode(cfg)); out:close()
    local state = not cfg.enabled and 'ist AUS.' or (cfg.dry_run and 'läuft im PROBELAUF (es wird nichts exportiert).'
      or 'ist AKTIV und exportiert wirklich.')
    LrDialogs.message('MHPhotos Auto-Export', 'Gespeichert. Der Auto-Export ' .. state ..
      '\nStatus: ~/.mhphotos-lightroom-mcp/autoexport-status.txt', 'info')
  end)
  trace('setup: done')
end

trace('menu script started')
LrTasks.startAsyncTask(function()
  local ok, err = LrTasks.pcall(setup)
  if not ok then
    local msg = type(err) == 'table' and tostring(err.message) or tostring(err)
    trace('setup FAILED: ' .. msg)
    LrDialogs.message('MHPhotos Auto-Export: Fehler', msg .. '\n\n(Protokoll: ~/.mhphotos-lightroom-mcp/autoexport-setup.log)', 'critical')
  end
end)
