# Larger collection snapshots and paged reads

The Lightroom preview now enumerates at most 1,000 names and hierarchy entries.
The eight-level hierarchy and cycle protections remain. The preview still shows
15 names and explicitly states how many entries will be sent and whether the
snapshot is incomplete. Sending requires the existing explicit confirmation.
No photos, smart-collection rules, catalog identifiers or write operations are
accessed. Collection names are private user data.

The private helper validates a maximum 1 MiB collection transfer file. Photo
transfers retain their separate 64 KiB limit. The bridge replaces the entire
collection snapshot atomically after successful validation, keeps it only in
memory and expires it after five minutes. An older bridge is rejected before
upload, using its advertised collection capacity.

`collection_overview` retains its existing result for snapshots of 100 or fewer
entries. For larger snapshots it returns the first 100 with the pagination fields
below, rather than an unbounded MCP result.

`collection_overview_page(offset=0, limit=100, snapshot_id=None)` returns:

- `collections`: at most 100 records;
- `total_count`: the number of submitted records;
- `offset` and `next_offset` (null on the last page);
- `snapshot_id`: a SHA-256 identifier of the submitted snapshot content;
- `truncated`: whether Lightroom omitted names because of the count/depth/cycle
  limits. This is independent of `next_offset`;
- `source`: `local_bridge_snapshot`.

Pass the returned `snapshot_id` and `next_offset` for subsequent pages. A changed
snapshot returns `snapshot_changed`; an expired snapshot returns `no_collections`.
Restart from offset zero in either case. Identical content has the same identifier,
so an identical replacement remains safe to page. Parent IDs can refer to records
on earlier pages and are scoped to this snapshot, not persistent catalog IDs.
Malformed page arguments are rejected before contacting the bridge.

Both stdio and HTTP expose the new read-only tool. HTTP retains the existing
`lightroom:read` scope and read-only annotations. Production deployment and
credentials are unchanged by these source changes.
