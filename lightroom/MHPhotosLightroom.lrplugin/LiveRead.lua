-- Fixed read-only requests. No eval, write gates, paths, rules or catalog commands.
local Tasks = import "LrTasks"
local Metadata = require "LiveMetadata"
local Collections = require "CollectionMetadata"
local Live = {}
local currentCatalog, sessionId

local function unhex(text)
    if #text == 0 or #text > 160 or #text % 2 ~= 0 or text:find('[^0-9a-f]') then error('Invalid name') end
    return (text:gsub('..', function(pair) return string.char(tonumber(pair,16)) end))
end

function Live.parse(text)
    if text:sub(-1)~='\n' or #text > 2048 then error('Invalid request') end
    local lines = {}
    for line in text:gmatch('([^\n]*)\n') do lines[#lines+1]=line end
    if #lines < 6 or #lines > 14 or #lines[1] ~= 32 or lines[1]:find('[^0-9a-f]') then error('Invalid request') end
    local request = {id=lines[1],operation=lines[2],offset=tonumber(lines[3]),limit=tonumber(lines[4]),previews=lines[5]=='1',session=lines[6],path={}}
    if lines[5] ~= '0' and lines[5] ~= '1' then error('Invalid previews') end
    if request.session ~= '' and (#request.session~=32 or request.session:find('[^0-9a-f]')) then error('Invalid session') end
    if request.operation == 'collections' then
        if #lines~=6 or request.offset~=0 or request.limit~=0 or request.previews or request.session~='' then error('Invalid request') end
    elseif request.operation == 'collection_photos' then
        if #lines<7 or not request.offset or request.offset<0 or request.offset>1000000 or request.offset%1~=0
            or not request.limit or request.limit<1 or request.limit>(request.previews and 10 or 50) or request.limit%1~=0 then error('Invalid request') end
        for i=7,#lines do
            local name=unhex(lines[i])
            if name:find('[%c]') then error('Invalid name') end
            request.path[#request.path+1]=name
        end
    else error('Unsupported operation') end
    return request
end

local function resolve(catalog, path)
    local container=catalog
    for index,name in ipairs(path) do
        local matches={}
        local function scan(nodes,isSet)
            for _,node in ipairs(nodes) do
                if node:getName()==name then matches[#matches+1]={node=node,isSet=isSet} end
            end
        end
        scan(container:getChildCollections(),false)
        scan(container:getChildCollectionSets(),true)
        if #matches==0 then return nil,'collection_not_found' end
        if #matches>1 then return nil,'collection_ambiguous' end
        if index<#path and not matches[1].isSet then return nil,'collection_not_found' end
        if index==#path and matches[1].isSet then return nil,'unsupported_collection' end
        container=matches[1].node
    end
    return container
end

local alphabet='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
local function base64(data)
    local result={}
    for i=1,#data,3 do
        local a,b,c=data:byte(i,i+2); b=b or 0; c=c or 0
        local n=a*65536+b*256+c
        local indices={math.floor(n/262144)%64, math.floor(n/4096)%64, math.floor(n/64)%64, n%64}
        result[#result+1]=alphabet:sub(indices[1]+1,indices[1]+1)..alphabet:sub(indices[2]+1,indices[2]+1)
            ..(i+1<=#data and alphabet:sub(indices[3]+1,indices[3]+1) or '=')
            ..(i+2<=#data and alphabet:sub(indices[4]+1,indices[4]+1) or '=')
    end
    return table.concat(result)
end
Live.base64=base64

local pending, pendingCount={},0
-- Start requests together so rendering gets one bounded batch deadline.
local function requestPreview(photo,index)
    local slot={index=index,finished=false}
    if pendingCount>=10 then slot.finished=true; slot.reason='preview_unavailable'; return slot end
    pending[slot]=true; pendingCount=pendingCount+1
    local ok,handle=Tasks.pcall(function()
        return photo:requestJpegThumbnail(512,512,function(data)
            slot.finished=true
            slot.handle=nil
            if not slot.abandoned then
                if type(data)~='string' or #data==0 then slot.reason='preview_unavailable'
                elseif #data>512000 then slot.reason='preview_size_limit'
                else slot.bytes=data end
            end
            if pending[slot] then pending[slot]=nil; pendingCount=pendingCount-1 end
        end)
    end)
    if not ok then
        if pending[slot] then pending[slot]=nil; pendingCount=pendingCount-1 end
        slot.finished=true; slot.reason='preview_unavailable'
    elseif not slot.finished then
        slot.handle=handle
    end
    return slot
end

local function finishPreviews(slots, startedAt)
    -- Measure elapsed wall time: SDK sleeps can resume much later than requested.
    -- Keep a finite iteration cap as a second bound if the clock moves backwards.
    for _=1,300 do
        local finished=true
        for _,slot in ipairs(slots) do if not slot.finished then finished=false; break end end
        local elapsed=os.time()-startedAt
        if finished or elapsed<0 or elapsed>=15 then break end
        Tasks.sleep(0.05)
    end
    local encoded={}
    for _,slot in ipairs(slots) do
        if not slot.finished then slot.reason='preview_timeout'; slot.abandoned=true end
        if slot.reason then
            encoded[#encoded+1]='{"index":'..slot.index..',"error":"'..slot.reason..'"}'
        else
            encoded[#encoded+1]='{"index":'..slot.index..',"jpeg":"'..base64(slot.bytes)..'"}'
        end
        slot.bytes=nil
    end
    return encoded
end

function Live.execute(catalog,request)
    if catalog~=currentCatalog then currentCatalog=catalog; sessionId=request.id end
    if request.session~='' and request.session~=sessionId then return '{"error":"catalog_changed"}' end
    if request.operation=='collections' then
        local encoded=Collections.encode(Collections.read(catalog))
        return encoded:sub(1,-2)..',"session_id":"'..sessionId..'"}'
    end
    local collection,reason=resolve(catalog,request.path)
    if not collection then return '{"error":"'..reason..'"}' end
    local photos=collection:getPhotos()
    if type(photos)~='table' or #photos>1000000 or request.offset>#photos then error('Invalid photos') end
    local records,previews={},{}
    local startedAt=os.time()
    for i=request.offset+1,math.min(#photos,request.offset+request.limit) do
        records[#records+1]=Metadata.read(photos[i])
        if request.previews then
            local elapsed=os.time()-startedAt
            if elapsed<0 or elapsed>=15 then
                previews[#previews+1]={index=#records,finished=true,reason='preview_timeout'}
            else
                previews[#previews+1]=requestPreview(photos[i],#records)
            end
        end
    end
    if request.previews then previews=finishPreviews(previews,startedAt) end
    local encoded=Metadata.encode(records)
    return encoded:sub(1,-2)..',"total_count":'..#photos..',"offset":'..request.offset
        ..',"has_more":'..tostring(request.offset+#records<#photos)
        ..',"previews":['..table.concat(previews,',')..'],"session_id":"'..sessionId..'"}'
end
return Live
