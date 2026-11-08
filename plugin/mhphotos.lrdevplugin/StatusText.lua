-- Text for the Status dialog from the numbers the loop keeps in plugin-status.json.
local StatusText = {}

local function ago(now, t)
  if not t then return 'noch nie' end
  local s = math.max(0, now - t)
  if s < 90 then return 'vor ' .. s .. ' s' end
  if s < 5400 then return 'vor ' .. math.floor(s / 60) .. ' min' end
  return 'vor ' .. math.floor(s / 3600) .. ' h'
end

-- info: decoded plugin-status.json (or nil), extra: { catalog = path, hooks = n, now = os.time() }
function StatusText.format(info, extra)
  extra = extra or {}
  local now = extra.now or os.time()
  if type(info) ~= 'table' then
    return 'Noch keine Statusdatei. Läuft der MCP-Dienst und ist das Plugin neu geladen?'
  end
  local lines = {
    'Plugin-Version: ' .. tostring(info.version or '?'),
    'Verbindung: ' .. tostring(info.status or '?'),
    'Befehle: ' .. tostring(info.commands or 0) .. ' erledigt, ' .. tostring(info.errors or 0) .. ' mit Fehler',
    'Letzter Befehl: ' .. tostring(info.last_command or '-') .. ' (' .. ago(now, info.last_ok_at) .. ')',
  }
  if info.started_at then lines[#lines + 1] = 'Läuft seit: ' .. ago(now, info.started_at):gsub('^vor ', '') end
  if (tonumber(info.takeovers) or 0) > 0 then
    lines[#lines + 1] = 'Selbstheilung: ' .. tostring(info.takeovers) .. 'x neu verbunden (zuletzt '
      .. ago(now, info.last_takeover_at) .. ', ' .. tostring(info.last_takeover or '?') .. ')'
  end
  if extra.catalog then lines[#lines + 1] = 'Katalog: ' .. tostring(extra.catalog) end
  if extra.hooks then lines[#lines + 1] = 'Webhooks eingerichtet: ' .. tostring(extra.hooks) end
  if type(info.recent_errors) == 'table' and #info.recent_errors > 0 then
    lines[#lines + 1] = ''
    lines[#lines + 1] = 'Letzte Fehler:'
    for i = math.max(1, #info.recent_errors - 4), #info.recent_errors do
      local e = info.recent_errors[i]
      lines[#lines + 1] = ' - ' .. tostring(e.command or '?') .. ': ' .. tostring(e.message or '?')
    end
  end
  return table.concat(lines, '\n')
end

return StatusText
