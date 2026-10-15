-- Shared mutable state (require() caches modules, so all files see the same table).
return { stop = false, status = 'starting', lastCommand = nil }
