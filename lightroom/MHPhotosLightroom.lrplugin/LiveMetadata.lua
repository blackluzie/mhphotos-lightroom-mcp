-- Search context for explicitly requested collection photos only; no GPS or paths.
local Tasks=import 'LrTasks'
local Base=require 'SelectionMetadata'
local Context={}
local textFields={'keywordTagsForExport','title','caption'}
local locationFields={'location','city','stateProvince','country'}
local allFields={'keywordTagsForExport','title','caption','location','city','stateProvince','country'}
local function bounded(text)
    if type(text)~='string' or text=='' then return nil end
    text=text:gsub('[%c]',' ')
    local finish=math.min(#text,80)
    while finish>0 and finish<#text do
        local byte=text:byte(finish+1)
        if byte<128 or byte>=192 then break end
        finish=finish-1
    end
    return text:sub(1,finish)
end
local function add(photo,result,fields)
    for _,key in ipairs(fields) do
        local ok,value=Tasks.pcall(function() return photo:getFormattedMetadata(key) end)
        if ok then result[key]=bounded(value) end
    end
end
function Context.read(photo)
    local result=Base.read(photo)
    add(photo,result,textFields)
    local ok,isPrivate=Tasks.pcall(function() return photo:getRawMetadata('locationIsPrivate') end)
    -- Missing/unsupported privacy information fails closed for location labels.
    if ok and isPrivate==false then add(photo,result,locationFields) end
    return result
end
local function quote(text)
    return '"'..text:gsub('[\\"]',function(c) return '\\'..c end)..'"'
end
function Context.encode(records)
    local values={}
    for _,record in ipairs(records) do
        local base=Base.encode({record}):sub(12,-3)
        local additions={}
        for _,key in ipairs(allFields) do
            if record[key]~=nil then additions[#additions+1]=quote(key)..':'..quote(record[key]) end
        end
        if #additions>0 then base=base:sub(1,-2)..','..table.concat(additions,',')..'}' end
        values[#values+1]=base
    end
    return '{"photos":['..table.concat(values,',')..']}'
end
return Context
