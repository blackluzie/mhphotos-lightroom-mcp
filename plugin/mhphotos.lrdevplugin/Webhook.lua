-- Calls to webhooks (for example n8n) from inside Lightroom's plug-in, for the MCP tool send_webhook.
--
-- Safety: the URLs live only in ~/.mhphotos-lightroom-mcp/webhooks.json on this Mac, never in a tool call.
-- A client can only name a hook ("n8n-import"), never choose an address. Only https:// (or http:// to
-- this machine) is accepted, and a payload is limited to 50 KB.
--
--   { "hooks": { "n8n-import": { "url": "https://n8n.example.com/webhook/abc", "token": "optional bearer token" } } }

local Webhook = {}

local MAX_PAYLOAD = 50000
local NAME = '^[a-z0-9][a-z0-9_-]*$'

local function allowedUrl(url)
  if type(url) ~= 'string' or #url > 500 or url:find('%s') then return false end
  if url:sub(1, 8) == 'https://' and #url > 8 then return true end
  for _, prefix in ipairs({ 'http://127.0.0.1', 'http://localhost' }) do
    local rest = url:sub(#prefix + 1, #prefix + 1)
    if url:sub(1, #prefix) == prefix and (rest == '' or rest == ':' or rest == '/') then return true end
  end
  return false
end

-- Decoded JSON -> { name = { url, token } } plus the list of problems (bad entries are skipped).
function Webhook.normalize(raw)
  local hooks, problems = {}, {}
  if type(raw) ~= 'table' or type(raw.hooks) ~= 'table' then return hooks, { 'no "hooks" table' } end
  for name, h in pairs(raw.hooks) do
    if type(name) ~= 'string' or #name > 40 or not name:match(NAME) then
      problems[#problems + 1] = 'bad hook name: ' .. tostring(name)
    elseif type(h) ~= 'table' or not allowedUrl(h.url) then
      problems[#problems + 1] = name .. ': url must be https:// (or http://localhost)'
    else
      hooks[name] = { url = h.url, token = type(h.token) == 'string' and h.token ~= '' and h.token or nil }
    end
  end
  return hooks, problems
end

function Webhook.names(hooks)
  local out = {}
  for name in pairs(hooks) do out[#out + 1] = name end
  table.sort(out)
  return out
end

-- deps: encode(table) -> text, post(url, body, headers, timeout) -> body, status.
function Webhook.send(hooks, name, payload, deps)
  local hook = hooks[name]
  if not hook then error('unknown webhook: ' .. tostring(name), 0) end
  local body = deps.encode(payload or {})
  if #body > MAX_PAYLOAD then error('payload too large (max 50 KB)', 0) end
  local headers = { { field = 'Content-Type', value = 'application/json' } }
  if hook.token then headers[#headers + 1] = { field = 'Authorization', value = 'Bearer ' .. hook.token } end
  local response, status = deps.post(hook.url, body, headers, 15)
  if status == nil then error('webhook did not answer (no connection)', 0) end
  return { name = name, http_status = status, ok = status >= 200 and status < 300, response = response and response:sub(1, 500) or nil }
end

return Webhook
