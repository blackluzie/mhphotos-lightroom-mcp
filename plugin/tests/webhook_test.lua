-- Webhook config checks and sending, against fake HTTP.
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path
local Webhook = require 'Webhook'

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

local hooks, problems = Webhook.normalize({ hooks = {
  ['n8n-import'] = { url = 'https://n8n.example.com/webhook/abc', token = 'sekret' },
  lokal = { url = 'http://127.0.0.1:5678/webhook/x' },
  localhost2 = { url = 'http://localhost/webhook/x' },
  klartext = { url = 'http://example.com/webhook' },
  evil = { url = 'http://127.0.0.1.evil.com/x' },
  ['Bad Name'] = { url = 'https://x.example.com' },
  nospace = { url = 'https://x.example.com/a b' },
  empty = { url = 'https://' },
} })
eq(table.concat(Webhook.names(hooks), ','), 'localhost2,lokal,n8n-import')
eq(#problems, 5)
eq(hooks['n8n-import'].token, 'sekret'); eq(hooks.lokal.token, nil)
local none, why = Webhook.normalize(nil)
eq(next(none), nil); eq(#why, 1)

local sent
local deps = {
  encode = function(t) return t.big and string.rep('x', 60000) or '{"ok":1}' end,
  post = function(url, body, headers, timeout) sent = { url = url, body = body, headers = headers, timeout = timeout }; return 'accepted', 200 end,
}
local res = Webhook.send(hooks, 'n8n-import', { a = 1 }, deps)
eq(res.ok, true); eq(res.http_status, 200); eq(res.response, 'accepted')
eq(sent.url, 'https://n8n.example.com/webhook/abc'); eq(sent.timeout, 15)
eq(sent.headers[2].field, 'Authorization'); eq(sent.headers[2].value, 'Bearer sekret')
Webhook.send(hooks, 'lokal', {}, deps); eq(#sent.headers, 1)
eq(pcall(Webhook.send, hooks, 'unbekannt', {}, deps), false)
eq(pcall(Webhook.send, hooks, 'lokal', { big = true }, deps), false)
deps.post = function() return nil, nil end
eq(pcall(Webhook.send, hooks, 'lokal', {}, deps), false)
deps.post = function() return 'boom', 500 end
local failed = Webhook.send(hooks, 'lokal', {}, deps)
eq(failed.ok, false); eq(failed.http_status, 500)
print('webhook ok')
