# Numbered collection preview sheets

`lightroom_collection_contact_sheet` returns current collection metadata and one
native JPEG image containing up to ten numbered previews. It is available only
through the isolated Mac test profile's stdio MCP server. The existing public
HTTP/OAuth registration does not expose this tool.

Example:

```json
{
  "collection_path": ["Travel", "Autumn"],
  "offset": 0,
  "limit": 10
}
```

The tool makes one existing `collection_photos` read with previews enabled. No
new Lightroom operation, permission or metadata field is introduced. The usual
worker opt-in and session checks apply. Pass the returned `session_id` for later
pages. Membership and order can change between reads.

Output contains a metadata text block and, when at least one preview is
available, one native `image/jpeg` block. `contact_sheet.labels` maps each visible
one-based photo number to its zero-based `collection_offset` and preview status.
The original `photos` and `preview_status` metadata remain in collection order.
Labels are read positions, not stable identities, ratings or editing handles.

Missing previews keep their original cell and display an explicit message.
Later previews never move into missing slots. If all previews are missing or the
collection is empty, the result contains text only with `available: false`.
A first render can time out; a later explicit read can retrieve completed
previews. No automatic retries are introduced.

The sheet has at most five columns and two rows (1280 × 408 pixels). Each photo
is fit inside its cell, preserving its aspect ratio. No source file is opened
or changed: the renderer uses only the authorized JPEG bytes already returned
by the preview tool. Decode dimensions, input counts/bytes and output bytes are
bounded. A fresh RGB canvas is encoded without embedded EXIF, ICC or comments.
No raw exception text or private filesystem paths are returned on rendering
failure, and the renderer writes no files.

Use the sheet for visual comparison, then request a candidate's larger individual
preview with `lightroom_collection_photos`, using the same path/session and its
`collection_offset`. Check the fresh metadata before associating it with an
earlier sheet. No automatic best-photo ranking, catalog selection, collection
changes, ratings, Develop operations or presets are performed.

Tests cover partial/missing previews, index mappings, pixel order, maximum image
size, metadata removal, invalid/non-JPEG/oversize input, native MCP images,
backend errors, pre-fetch input rejection and public HTTP exclusion.
