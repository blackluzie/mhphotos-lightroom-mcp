-- Version.lua (shown in the Status dialog) must match VERSION in Info.lua.
package.path = '../mhphotos.lrdevplugin/?.lua;' .. package.path
local Version = require 'Version'
local info = dofile('../mhphotos.lrdevplugin/Info.lua')
local v = info.VERSION
local text = v.major .. '.' .. v.minor .. '.' .. v.revision
if text ~= Version then error('Info.lua says ' .. text .. ' but Version.lua says ' .. Version) end
print('version ok ' .. Version)
