# Search submitted collection names

`collection_search(query, kind=None, offset=0, limit=100, snapshot_id=None)`
searches the explicitly submitted in-memory overview. It never contacts Lightroom
or enumerates a catalog. The original photo bridge and selection flow are unchanged.

The search is a case-insensitive literal substring of the collection name and its
ancestor names (joined with ` / `). Unicode case folding is supported. Query text
is trimmed, limited to 80 characters and rejects control characters. It is never
interpreted as regex, shell commands, catalog identifiers or Smart collection rules.
An empty query is allowed only with a kind filter:
`collection`, `smart_collection`, or `collection_set`.

Results preserve distinct snapshot IDs for duplicate names and include a
`collection_path` list with ancestor names followed by the entry's name.
`total_count` counts matching entries; `snapshot_count` counts all submitted
entries. Source `truncated` remains visible even with zero matches. No matches
means only that the current submitted overview has no match.

Each page is bounded to 100 entries. Pass the same query/filter together with the
returned `next_offset` and `snapshot_id` to read further pages. The identifier
covers the complete overview and the normalized query/filter. Changes to an
unmatched entry also invalidate continuation. Changed query/filter or snapshot
returns `snapshot_changed`. Five-minute expiry returns `no_collections`; restart
after explicitly sending a new overview. Nothing extends snapshot lifetime.

The tool is published over stdio and HTTP with existing `lightroom:read` scope,
read-only, non-destructive annotations. It grants no additional catalog access or
writes. No Lua/plugin change or Lightroom reload is required for this MCP-only
function once the separate test helper package is updated.
