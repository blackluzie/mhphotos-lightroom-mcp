# Direct read-only requests in the isolated Mac profile

This adds a chat → loopback job broker → opt-in Lightroom worker → MCP response
path. Manual Send actions and their five-minute RAM snapshots remain independent
and unchanged. The default/production bridge refuses live endpoints. Only the
private profile bridge enables the broker; no public listener, OAuth grant or
production service is changed.

## Enable and stop

In the separate Mac test plugin, use **Start Read-only Connection** and confirm
the concrete read scope once for the current Lightroom session. Cancel does not
start any worker or read the catalog. There is no automatic startup or catalog
scan. Keep Lightroom open. **Stop Read-only Connection** disables the worker and
cancels broker jobs; plugin shutdown also stops it. Reload requires enabling again.
The SDK polling loop asks only for queued requests, not catalog data when idle.

Read scope: ordinary/Smart collection names/hierarchy, photos of an exact name
path, the existing bounded metadata fields, exportable keywords/title/caption,
non-private textual location labels and JPEG previews. GPS, paths, hidden keyword
export data, search rules, contact/serial fields and complete metadata tables are
not read. Location labels require an explicitly false `locationIsPrivate`; failed
or unknown privacy getters omit them. Context display text is UTF-8-safe and
bounded to 80 bytes, so it can be incomplete and must not establish definitive
absence of a location. Region lookups can use this context; image quality choices
require actual available previews, not filename/metadata-only guesses.

## MCP tools

The two new tools are **stdio/private profile only**, intentionally absent from
the existing public HTTP tool registry. Public connector rollout with appropriate
consent is a separate step after real Mac verification.

- `lightroom_collections(query="")`: request current bounded names (maximum 1,000,
  depth eight), then optionally match names/ancestors case-insensitively. Return
  at most 100 matches with name paths, match count, source truncation, more-matches
  indicator and a catalog session identifier. Use a specific query to narrow
  large results; never claim complete coverage if `truncated` is true.
- `lightroom_collection_photos(collection_path, offset=0, limit=10,
  include_previews=True, session_id=None)`: exact name segments, not filesystem
  paths. Request at most ten photos with previews or fifty without. Return only
  their metadata and native MCP JPEG image blocks, total collection count and
  whether more photos exist. SDK reads never change the current selection.
  Duplicate names under the same parent are rejected as ambiguous; collection
  sets and published sources are not treated as ordinary photo collections.

Pass the returned session identifier for subsequent pages to reject a switched
or reopened catalog. Each page is a fresh read; membership/order/edits may change
between calls, so pages are not a frozen catalog snapshot or stable editing IDs.
Names longer than 80 UTF-8 bytes/control-character names cannot be addressed by
this initial bounded name-path protocol. Error/timeout/missing-preview states are
explicit. No matching/ranking automatically writes ratings, picks or Develop data.

## Boundaries

- IPv4 loopback, private bearer credentials, forbidden cross-origin requests.
- Only `collections` and `collection_photos` are accepted. No arbitrary SDK method,
  Lua source, shell command, preset operation or catalog write can be queued.
- At most two outstanding jobs, thirty-second deadline, UUID request identifiers,
  single claim/reply, expired/replayed replies rejected (409 `live_request_unavailable`); results removed from RAM
  after delivery/timeout. Heartbeat is worker availability, not a catalog sync.
- Private owner-only temporary files in the existing isolated transfers folder.
  Poll commands use fixed lines/hex-encoded names and are never evaluated as Lua.
  Result files are removed after processing; stdout errors contain no credentials
  or SDK exception/path text. Paths and executable arguments are shell-quoted.
- Bodies bounded to 4 KiB for requests, 8 MiB for preview replies. JPEG sources are
  at most 512,000 bytes and 4,096 pixels per side; output is at most 1,024 pixels
  per side. Decode JPEG only and re-encode a fresh pixel-only object, discarding
  EXIF/XMP/comments/ICC data. Originals are untouched. These are previews, not
  full-resolution files or a color-proofed export.
- The SDK thumbnail handle is retained until callback, including timeout cases;
  outstanding late callbacks are bounded to ten. A missing/slow/large preview
  returns a per-photo error, not a fabricated image.

## Later editing and presets

The broker currently rejects every write operation, including presets. A future
editing path needs stable scoped photo handles, a concrete previewable plan,
explicit approval, SDK write gates, rollback/history handling and separately
reviewed access/consent. Rating changes remain disabled. This PR does not apply
or prepare unapproved changes to user photos.

SDK references: Adobe-authored [collection reads](https://lrc.mcor.dev/modules/LrCollection.html)
and [metadata/JPEG requests](https://lrc.mcor.dev/modules/LrPhoto.html), hosted as
reference mirrors. Image processing follows [Pillow's security guidance](https://pillow.readthedocs.io/en/stable/handbook/security.html).

### Preview scheduling budget

The Mac worker measures actual elapsed wall time for the shared fifteen-second
preview wait, including thumbnail request startup. Counting requested short
sleeps is insufficient when Lightroom resumes tasks late. A finite iteration
cap remains as a fallback, and a backwards clock jump ends the wait. A blocked
SDK call or delayed task resumption itself cannot be preempted; this budget
bounds additional waiting, not the duration of Lightroom's own blocking work.
Late callbacks retain the existing discard/release behavior. Authentication,
job expiry, preview size limits and read-only permissions are unchanged.

A well-formed reply whose job is expired, stopped, already completed or no longer
claimed is rejected with HTTP 409 and exactly `{"error":"live_request_unavailable"}`.
The helper discards that response, deletes its private result file and continues
polling; no late data are delivered, cached or retried. It recognizes only this
bounded exact response on the reply route. Authentication, origin, invalid result,
other status/body and transport failures still stop the worker. Malformed job
identifiers remain invalid payloads. This prevents a delayed SDK result from
turning one request timeout into a session-wide disconnect.

### Repeated reads while thumbnails are pending

An explicit read reuses a still-pending thumbnail request for the same SDK photo
object and catalog object. It does not enqueue another rendering request or use
a collection position/name as photo identity. The current read still collects
fresh metadata and assigns current page indices. The ten-pending-request cap and
page budget remain in force. A callback arriving during the later explicit wait
can satisfy that read. A callback arriving with no active consumer discards its
bytes and releases the slot; a future read starts afresh. No completed JPEG cache,
automatic retries, background catalog scan or extra metadata access is added.
Repeated callbacks after completion are ignored. Catalog changes never reuse an
old catalog's pending slot. SDK callbacks that never complete can still occupy
bounded slots; the tool does not claim it can cancel Lightroom rendering.
