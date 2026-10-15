-- Opt-in, isolated Mac worker. Only LiveRead's fixed read operations are available.
local Tasks=import 'LrTasks'
local Files=import 'LrFileUtils'
local Paths=import 'LrPathUtils'
local Dialogs=import 'LrDialogs'
local Application=import 'LrApplication'
local Live=require 'LiveRead'
local Worker={running=false,generation=0}
local function quote(value) return "'"..value:gsub("'","'\\''").."'" end

function Worker.stop()
    local wasRunning=Worker.running
    Worker.running=false; Worker.generation=Worker.generation+1
    if wasRunning and Worker.installation then
        local installation=Worker.installation
        Tasks.startAsyncTask(function()
            Tasks.execute(quote(installation.python)..' -I -m mhphotos_lightroom_mcp.profile_runner '
                ..quote(installation.profileRoot)..' worker-stop')
        end)
    end
end
function Worker.start()
    if Worker.running then Dialogs.message('MHPhotos','Read-only connection is already enabled.','info'); return end
    if WIN_ENV then Dialogs.message('MHPhotos','This live test profile currently requires macOS.','warning'); return end
    local installation=require 'Installation'
    if type(installation.profileRoot)~='string' or type(installation.python)~='string' then
        Dialogs.message('MHPhotos','The separate updated Mac test profile is required.','warning'); return
    end
    local directory=Paths.child(installation.profileRoot,'transfers')
    if not Files.exists(directory) then Dialogs.message('MHPhotos','Private profile directory missing.','warning'); return end
    if Dialogs.confirm('MHPhotos Read-only Connection',
        'Allow chat requests to read collection names, photo metadata (including exportable keywords, titles, captions and non-private location labels) and JPEG previews while Lightroom is open? '
        ..'No ratings, photos, presets or catalog settings will be changed. '
        ..'Use Stop Read-only Connection to disable access.','Enable read-only connection','Cancel')~='ok' then return end
    Worker.installation=installation
    Worker.generation=Worker.generation+1
    local generation=Worker.generation
    Worker.running=true
    Tasks.startAsyncTask(function()
        while Worker.running and generation==Worker.generation do
            local filename=Paths.child(directory,'live-'..os.time()..'-'..math.random(100000,999999)..'.txt')
            local function execute(mode)
                local prefix=mode=='reply' and '/bin/chmod 600 '..quote(filename)..' && ' or ''
                return Tasks.execute(prefix..quote(installation.python)
                    ..' -I -m mhphotos_lightroom_mcp.profile_runner '..quote(installation.profileRoot)
                    ..' '..mode..' '..quote(filename))
            end
            if not Files.exists(filename) then
                local stage='poll_helper'
                local success=Tasks.pcall(function()
                    if execute('poll')~=0 then error('Poll failed') end
                    stage='request_file'
                    local file=io.open(filename,'r')
                    if not file then error('Missing request') end
                    local text,readError=file:read(2049); file:close(); Files.delete(filename)
                    if readError then error('Cannot read request') end
                    -- Lua returns nil at EOF for the empty idle-poll file.
                    if text==nil or text=='' then return end
                    stage='request_parse'
                    local request=Live.parse(text)
                    local ok,result=Tasks.pcall(function()
                        if not Worker.running or generation~=Worker.generation then return '{"error":"worker_stopped"}' end
                        local catalog=Application.activeCatalog()
                        if not catalog then error('No catalog') end
                        return Live.execute(catalog,request)
                    end)
                    if not ok then result='{"error":"sdk_error"}' end
                    if not Worker.running or generation~=Worker.generation then result='{"error":"worker_stopped"}' end
                    stage='reply_file'
                    local output=io.open(filename,'w')
                    if not output then error('Cannot create response') end
                    local written=output:write('{"id":"'..request.id..'","result":'..result..'}')
                    output:close()
                    if not written then error('Cannot write response') end
                    stage='reply_helper'
                    if execute('reply')~=0 then error('Reply failed') end
                end)
                if Files.exists(filename) then Files.delete(filename) end
                if not success then
                    Worker.running=false
                    Dialogs.message('MHPhotos Read-only Connection',
                        'Read-only connection stopped at '..stage..'. Please report this code. No catalog writes.', 'warning')
                end
            end
            Tasks.sleep(2)
        end
    end)
    Dialogs.message('MHPhotos','Read-only connection enabled for this Lightroom session. No catalog writes.','info')
end
return Worker
