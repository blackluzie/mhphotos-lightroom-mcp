package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path
local Json = require 'Json'

local function eq(a, b, msg)
  if a ~= b then error((msg or 'assert') .. ': expected ' .. tostring(b) .. ' got ' .. tostring(a), 2) end
end

-- roundtrip
local v = Json.decode('{"a":[1,2.5,-3],"b":{"c":"x\\ny"},"d":true,"e":null,"f":false}')
eq(v.a[2], 2.5); eq(v.a[3], -3); eq(v.b.c, 'x\ny'); eq(v.d, true); eq(v.f, false); eq(v.e, nil)

-- unicode: BMP, surrogate pair, raw utf-8 passthrough
eq(Json.decode('"\\u00e4"'), 'ä')
eq(Json.decode('"\\ud83d\\ude00"'), '😀')
eq(Json.encode('Müller'), '"Müller"')

-- encode escaping + arrays/empties/numbers
eq(Json.encode('a"b\\c\n\1'), '"a\\"b\\\\c\\n\\u0001"')
eq(Json.encode({}), '[]')
eq(Json.encode({ 1, 2, 3 }), '[1,2,3]')
eq(Json.encode({ x = 1 }), '{"x":1}')
eq(Json.encode(5), '5'); eq(Json.encode(0.5), '0.5')
eq(Json.encode({ ok = true, list = { 'a', 'b' } }):find('"list":%["a","b"%]') ~= nil, true)

-- decode errors
for _, bad in ipairs({ '{', '[1,', '{"a" 1}', '"abc', '1 2', 'nope', '{"a":1,}' }) do
  local ok = pcall(Json.decode, bad)
  eq(ok, false, 'should reject ' .. bad)
end

-- whitespace
eq(Json.decode(' \n[ 1 , 2 ]\t')[2], 2)
print('json ok')
