local Tasks=import 'LrTasks'
Tasks.startAsyncTask(function()
    local Worker=require 'LiveWorker'
    Worker.stop()
    local Dialogs=import 'LrDialogs'
    Dialogs.message('MHPhotos','Read-only connection stopped. No new catalog requests will be processed.','info')
end)
