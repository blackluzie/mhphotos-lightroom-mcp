# Search current collection photo metadata

The isolated Mac test profile exposes `lightroom_collection_photo_search` over
stdio MCP. It uses the existing authenticated `collection_photos` read operation,
with previews disabled. No new Lightroom operation or metadata field is added.
No service or plugin restart is needed when updating only this stdio tool.

Example first page:

```json
{
  "collection_path": ["Travel", "Autumn"],
  "query": "mountain",
  "scan_limit": 50
}
```

Search is a literal case-insensitive substring comparison, normalized to Unicode
NFC. Query text must be nonempty, at most 80 characters and contain no control
characters or invalid surrogate text. It matches any of these fields:

- `fileName`
- `keywordTagsForExport`
- `title`, `caption`
- `location`, `city`, `stateProvince`, `country`

The worker supplies only its existing bounded metadata. Longer values may be
truncated. Hidden keywords, GPS and private location labels are not accessed.
The tool does not infer the subject or place from an image, filename or city.
For example, a city name does not automatically match a regional search term.

Each call scans at most 50 photos, makes one bridge request, and returns:

- `photos`: matches containing `metadata`, `matched_fields` and zero-based
  `collection_offset` in the current collection order.
- `match_count`: matches on this page only.
- `scanned_count`, `offset`, `next_offset`: scan progress. A page with no matches
  can still have more photos to scan.
- `total_count`: collection size reported for this read.
- `session_id`, `collection_path`, `query`, `search_fields`, `source` and
  `catalog_writes: false`.

Continue using `next_offset`, the same query/path and the returned `session_id`.
Nonzero offsets require a catalog session. A switched catalog is rejected.
Membership and order can change between calls; this is not a frozen snapshot.
Zero matches does not establish that a place or subject is absent from the
images, even after all pages have been read.

For a match, request its preview using `lightroom_collection_photos` with the
same path/session and `offset: collection_offset`, `limit: 1`. An offset is a
position for a fresh read, not a stable photo identity or an editing handle.
Verify current metadata before associating a later preview with a prior match.
No ranking, ratings, collection changes, presets or image edits are performed.

Invalid input is rejected before contacting Lightroom. Bridge failures,
expired jobs, catalog changes and invalid results remain explicit. The tool is
excluded from public HTTP/OAuth registration, alongside the other live tools.

Validation includes literal/Unicode matching, field boundaries, duplicate/order
preservation, continuation, malformed input/results, switched catalogs,
read-only tool annotations, public HTTP exclusion, and a real authenticated
loopback bridge test using the unchanged worker operation.
