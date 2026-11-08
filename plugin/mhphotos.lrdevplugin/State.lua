-- Shared mutable state (require() caches modules, so all files see the same table).
-- active: name of the runner task that polls right now; deadline: os.time() by which it must report back
-- (Loop.lua's watchdog lets another runner take over after that).
return { stop = false, status = 'starting', lastCommand = nil, active = nil, deadline = 0 }
