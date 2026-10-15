local Dialogs=import "LrDialogs"
local Worker=require "LiveWorker"
local message=Worker.running and "Read-only request worker enabled for this Lightroom session."
    or "Read-only request worker disabled. Use Start Read-only Connection in the separate Mac test plugin."
Dialogs.message("MHPhotos Lightroom MCP",message.." Bridge availability is checked when a request is made. No catalog writes or rating changes.","info")
