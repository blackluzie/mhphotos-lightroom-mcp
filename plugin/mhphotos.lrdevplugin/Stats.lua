-- Counters of the polling loop, written to plugin-status.json for the Status dialog.
local Stats = {}

local MAX_ERRORS = 10

function Stats.new(version, now)
  return { version = version, status = 'starting', commands = 0, errors = 0, started_at = now, recent_errors = {} }
end

function Stats.finished(s, command, ok, err, now)
  s.last_command = command
  if ok then
    s.commands = s.commands + 1
    s.last_ok_at = now
  else
    s.errors = s.errors + 1
    s.recent_errors[#s.recent_errors + 1] = { command = command, message = tostring(err):sub(1, 200), at = now }
    while #s.recent_errors > MAX_ERRORS do table.remove(s.recent_errors, 1) end
  end
end

return Stats
