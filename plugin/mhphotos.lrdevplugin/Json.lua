-- Minimal JSON encoder/decoder (Lua 5.1, no dependencies).
-- Arrays: tables with keys 1..n. Empty tables encode as [] . null decodes to nil.

local Json = {}

local escapes = {
  ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
  ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t',
}

local function encodeString(s)
  s = s:gsub('[%c"\\]', function(c)
    return escapes[c] or string.format('\\u%04x', c:byte())
  end)
  return '"' .. s .. '"'
end

local function isArray(t)
  local n = 0
  for _ in pairs(t) do n = n + 1 end
  for i = 1, n do
    if t[i] == nil then return false end
  end
  return true
end

local function encode(v)
  local tv = type(v)
  if tv == 'nil' then return 'null' end
  if tv == 'boolean' then return tostring(v) end
  if tv == 'number' then
    if v ~= v or v == math.huge or v == -math.huge then return 'null' end
    if v == math.floor(v) and math.abs(v) < 2 ^ 53 then return string.format('%d', v) end
    return string.format('%.14g', v)
  end
  if tv == 'string' then return encodeString(v) end
  if tv == 'table' then
    local parts = {}
    if isArray(v) then
      for i = 1, #v do parts[#parts + 1] = encode(v[i]) end
      return '[' .. table.concat(parts, ',') .. ']'
    end
    for k, val in pairs(v) do
      parts[#parts + 1] = encodeString(tostring(k)) .. ':' .. encode(val)
    end
    return '{' .. table.concat(parts, ',') .. '}'
  end
  error('cannot encode ' .. tv)
end

function Json.encode(v) return encode(v) end

local function utf8char(cp)
  if cp < 0x80 then return string.char(cp) end
  if cp < 0x800 then
    return string.char(0xC0 + math.floor(cp / 0x40), 0x80 + cp % 0x40)
  end
  if cp < 0x10000 then
    return string.char(0xE0 + math.floor(cp / 0x1000),
      0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
  end
  return string.char(0xF0 + math.floor(cp / 0x40000),
    0x80 + math.floor(cp / 0x1000) % 0x40,
    0x80 + math.floor(cp / 0x40) % 0x40, 0x80 + cp % 0x40)
end

local function decodeError(str, pos, msg)
  error(string.format('JSON error at %d: %s', pos, msg), 0)
end

local function skip(str, pos)
  return str:find('[^ \t\r\n]', pos) or #str + 1
end

local decodeValue

local function decodeString(str, pos)
  local out, i = {}, pos + 1
  while true do
    local c = str:sub(i, i)
    if c == '' then decodeError(str, pos, 'unterminated string') end
    if c == '"' then return table.concat(out), i + 1 end
    if c == '\\' then
      local e = str:sub(i + 1, i + 1)
      local simple = { ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b',
        f = '\f', n = '\n', r = '\r', t = '\t' }
      if simple[e] then
        out[#out + 1] = simple[e]; i = i + 2
      elseif e == 'u' then
        local hex = str:sub(i + 2, i + 5)
        if not hex:match('^%x%x%x%x$') then decodeError(str, i, 'bad \\u escape') end
        local cp = tonumber(hex, 16)
        i = i + 6
        if cp >= 0xD800 and cp <= 0xDBFF then
          local lo = str:match('^\\u(%x%x%x%x)', i)
          if lo then
            local l = tonumber(lo, 16)
            if l >= 0xDC00 and l <= 0xDFFF then
              cp = 0x10000 + (cp - 0xD800) * 0x400 + (l - 0xDC00)
              i = i + 6
            end
          end
        end
        out[#out + 1] = utf8char(cp)
      else
        decodeError(str, i, 'bad escape')
      end
    else
      out[#out + 1] = c; i = i + 1
    end
  end
end

function decodeValue(str, pos)
  pos = skip(str, pos)
  local c = str:sub(pos, pos)
  if c == '{' then
    local obj = {}
    pos = skip(str, pos + 1)
    if str:sub(pos, pos) == '}' then return obj, pos + 1 end
    while true do
      pos = skip(str, pos)
      if str:sub(pos, pos) ~= '"' then decodeError(str, pos, 'expected string key') end
      local key
      key, pos = decodeString(str, pos)
      pos = skip(str, pos)
      if str:sub(pos, pos) ~= ':' then decodeError(str, pos, "expected ':'") end
      local val
      val, pos = decodeValue(str, pos + 1)
      obj[key] = val
      pos = skip(str, pos)
      local d = str:sub(pos, pos)
      if d == '}' then return obj, pos + 1 end
      if d ~= ',' then decodeError(str, pos, "expected ',' or '}'") end
      pos = pos + 1
    end
  elseif c == '[' then
    local arr, n = {}, 0
    pos = skip(str, pos + 1)
    if str:sub(pos, pos) == ']' then return arr, pos + 1 end
    while true do
      local val
      val, pos = decodeValue(str, pos)
      n = n + 1
      arr[n] = val
      pos = skip(str, pos)
      local d = str:sub(pos, pos)
      if d == ']' then return arr, pos + 1 end
      if d ~= ',' then decodeError(str, pos, "expected ',' or ']'") end
      pos = pos + 1
    end
  elseif c == '"' then
    return decodeString(str, pos)
  elseif str:sub(pos, pos + 3) == 'true' then return true, pos + 4
  elseif str:sub(pos, pos + 4) == 'false' then return false, pos + 5
  elseif str:sub(pos, pos + 3) == 'null' then return nil, pos + 4
  else
    local num = str:match('^-?%d+%.?%d*[eE]?[+-]?%d*', pos)
    if not num or num == '' then decodeError(str, pos, 'unexpected character') end
    local n = tonumber(num)
    if not n then decodeError(str, pos, 'bad number') end
    return n, pos + #num
  end
end

function Json.decode(str)
  local val, pos = decodeValue(str, 1)
  pos = skip(str, pos)
  if pos <= #str then decodeError(str, pos, 'trailing characters') end
  return val
end

return Json
