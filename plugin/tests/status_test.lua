-- Counters and text of the Status dialog.
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path
local Stats = require 'Stats'
local StatusText = require 'StatusText'

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

local s = Stats.new('0.1.18', 1000)
Stats.finished(s, 'list_photos', true, '', 1010)
Stats.finished(s, 'set_rating', false, 'write_blocked', 1020)
eq(s.commands, 1); eq(s.errors, 1); eq(s.last_command, 'set_rating'); eq(s.last_ok_at, 1010)
eq(s.recent_errors[1].command, 'set_rating')
for i = 1, 15 do Stats.finished(s, 'x' .. i, false, string.rep('e', 500), 1030 + i) end
eq(#s.recent_errors, 10, 'only the last ten errors are kept'); eq(#s.recent_errors[1].message, 200)

eq(StatusText.format(nil):find('Keine') ~= nil or StatusText.format(nil):find('Noch keine') ~= nil, true)
local text = StatusText.format(s, { now = 1100, catalog = '/K/Katalog.lrcat', hooks = 2 })
assert(text:find('Plugin%-Version: 0.1.18'), text)
assert(text:find('1 erledigt, 16 mit Fehler'), text)
assert(text:find('Letzter Befehl: x15'), text)
assert(text:find('Katalog: /K/Katalog.lrcat'), text)
assert(text:find('Webhooks eingerichtet: 2'), text)
assert(text:find('Letzte Fehler:'), text)
assert(text:find('vor 90 s') or text:find('vor 1 min'), text)
local fresh = StatusText.format(Stats.new('0.1.18', 50), { now = 60 })
assert(fresh:find('noch nie'), fresh)
assert(not text:find('Selbstheilung'), 'no watchdog line without takeovers')
s.takeovers, s.last_takeover_at, s.last_takeover = 2, 1040, 'runner-1 hing bei get_preview'
text = StatusText.format(s, { now = 1100 })
assert(text:find('Selbstheilung: 2x neu verbunden %(zuletzt vor 60 s, runner%-1 hing bei get_preview%)'), text)
print('status ok')
