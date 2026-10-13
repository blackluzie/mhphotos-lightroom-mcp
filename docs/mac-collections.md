# Read-only collection overview on Mac

The new Lightroom menu **MHPhotos: Preview and Send Collection Overview** reads
names and hierarchy for ordinary collections, Smart collections and collection
sets. It uses Adobe's read-only child-collection APIs and `getName`/
`isSmartCollection`; it never calls `getPhotos`, retrieves Smart collection rules,
looks up catalogue paths, or writes collection/catalogue data.

The dialogue shows the overview count and up to 15 entries. For larger overviews
it explicitly states that all listed entries will be sent, not only the previewed
entries. **Cancel** sends nothing. The separate **Send overview** action submits
only the explicit name hierarchy through the private local helper. No background
scan or automatic upload is added to the existing selection command.

## Boundaries

- At most **1,000 entries**, at most **eight hierarchy levels**, at most **1 MiB**
  encoded JSON. Collection names are bounded and truncated without splitting UTF-8.
- `truncated: true` explicitly identifies traversal limits/cycles. The overview
  is a bounded snapshot, not a complete or continuously connected catalog.
- Duplicate names remain separate records. IDs (`node1`, etc.) exist only within
  one snapshot and are not persistent Lightroom IDs. The questionable write-gate
  requirement documented for collection-set identifiers is avoided entirely.
- Published collections, publish destinations, folder paths, photos, rules,
  personal EXIF and catalogue filenames are not part of this projection.
- SDK errors abort the overview; they are not reported as an empty catalogue.
- Names are display text, never commands. Unknown fields, duplicate IDs,
  forward/cyclic parents, non-set parents and excessive depth/count are rejected.

## Transport and MCP

Authenticated `GET/POST /v1/collections` use the same IPv4-loopback/private-token
boundary as selection transfer. Capabilities advertise `collection_overview` and
`max_collections`; the helper refuses older bridges rather than dropping data.
The new installed helper/package is required; the current production installation
is not switched by this change.

Collection and photo snapshots are independent. A collection overview expires
and is cleared from memory after five minutes, without affecting photo selection.
Submitting an explicitly empty overview clears collection data while keeping
photo data. `connection_status` reports collection state, count and expiry.

The new MCP tool **`collection_overview`** reads only the submitted snapshot and
returns its truncation flag. No catalog enumeration is triggered by the MCP call.
HTTP tool annotations remain read-only/non-destructive with `lightroom:read`.
The next OAuth consent page includes explicitly submitted collection overviews.
Restarting the HTTP service invalidates previous sessions, so rollout requires
re-authentication with the updated consent page; no extra access is granted to a
running session by this development checkout.

## Validation and rollout

Tests execute the actual Lua traversal/JSON/dialogue logic in Lua 5.1 with mocked
SDK objects. They exercise nested/Smart/duplicate collections, empty catalogues,
UTF-8 boundaries, count/depth/cycle limits and cancellation before transmission.
HTTP/MCP tests cover authentication, forbidden origins, malformed graphs,
unknown/private fields, byte limits, unsupported bridges, file cleanup,
independent expiry and the absence of PUT/PATCH/DELETE mutation routes.

A real check in the separate Mac test profile retrieved 576 entries across six
MCP pages without source truncation. See [paging](mac-collection-pages.md) and
[search](mac-collection-search.md) for the current read tools.
The running bridge and installed plugin/helper are left unchanged. Ratings,
collection editing and other catalogue writes remain disabled. Windows manual
integration checks remain deferred while Mac development is prioritized.

Adobe-authored SDK references (hosted as a documentation mirror):
[LrCollection](https://lrc.mcor.dev/modules/LrCollection.html),
[LrCollectionSet](https://lrc.mcor.dev/modules/LrCollectionSet.html),
[LrCatalog](https://lrc.mcor.dev/modules/LrCatalog.html).
