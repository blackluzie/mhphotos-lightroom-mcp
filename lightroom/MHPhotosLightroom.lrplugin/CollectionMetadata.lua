-- Collection names and hierarchy only. Never fetch photos or search rules.
local Collections = {}
local MAX_NODES, MAX_DEPTH = 100, 8

local function nameOf(node)
    local name = node:getName()
    if type(name) ~= "string" or name == "" then error("Invalid collection name") end
    name = name:gsub("[%c]", " ")
    local finish = math.min(#name, 80)
    while finish > 0 and finish < #name do
        local nextByte = string.byte(name, finish + 1)
        if nextByte < 128 or nextByte >= 192 then break end
        finish = finish - 1
    end
    return name:sub(1, finish)
end

function Collections.read(catalog)
    local records, seen = {}, {}
    local truncated = false
    local visit
    local function children(container, parentId, depth)
        local regular = container:getChildCollections()
        local sets = container:getChildCollectionSets()
        if type(regular) ~= "table" or type(sets) ~= "table" then error("Invalid collection list") end
        for _, node in ipairs(regular) do
            if #records >= MAX_NODES then truncated = true; return end
            visit(node, false, parentId, depth)
        end
        for _, node in ipairs(sets) do
            if #records >= MAX_NODES then truncated = true; return end
            visit(node, true, parentId, depth)
        end
    end
    visit = function(node, isSet, parentId, depth)
        if seen[node] or depth > MAX_DEPTH then truncated = true; return end
        seen[node] = true
        local kind = "collection_set"
        if not isSet then
            local smart = node:isSmartCollection()
            if type(smart) ~= "boolean" then error("Invalid collection type") end
            kind = smart and "smart_collection" or "collection"
        end
        -- IDs are scoped to this snapshot; no catalog identifiers are accessed.
        local id = "node" .. tostring(#records + 1)
        records[#records + 1] = { id = id, name = nameOf(node), kind = kind, parentId = parentId }
        if isSet then children(node, id, depth + 1) end
    end
    children(catalog, nil, 1)
    return { collections = records, truncated = truncated }
end

local function quote(text)
    return '"' .. text:gsub('[%z\1-\31\\"]', function(c)
        if c == '"' then return '\\"' end
        if c == '\\' then return '\\\\' end
        return string.format("\\u%04x", string.byte(c))
    end) .. '"'
end

function Collections.encode(overview)
    local records = {}
    for _, record in ipairs(overview.collections) do
        local text = '{"id":' .. quote(record.id) .. ',"name":' .. quote(record.name)
            .. ',"kind":' .. quote(record.kind)
        if record.parentId then text = text .. ',"parentId":' .. quote(record.parentId) end
        records[#records + 1] = text .. "}"
    end
    return '{"collections":[' .. table.concat(records, ",") .. '],"truncated":'
        .. tostring(overview.truncated) .. '}'
end

function Collections.preview(overview)
    local lines = { string.format("Collection overview: %d entries", #overview.collections) }
    local depths = {}
    for index, record in ipairs(overview.collections) do
        if index > 15 then
            lines[#lines + 1] = string.format("Showing first 15 entries; sending all %d listed entries.", #overview.collections)
            break
        end
        local depth = record.parentId and (depths[record.parentId] + 1) or 0
        depths[record.id] = depth
        local label = record.kind == "smart_collection" and " [Smart]"
            or (record.kind == "collection_set" and " [Set]" or "")
        lines[#lines + 1] = string.rep("  ", depth) .. record.name .. label
    end
    if overview.truncated then lines[#lines + 1] = "Overview is incomplete (size/depth limit reached)." end
    lines[#lines + 1] = "Names only. No photos or collection rules read. No changes made."
    return table.concat(lines, "\n")
end
return Collections
