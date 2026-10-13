# Mac read-only metadata expansion

This development builds on the installer branch without replacing the deployed
Lightroom plugin, helper or running bridge. Installation/Windows release testing
is deferred while Mac functions are developed.

## Selection metadata

The existing fields `fileName`, `rating`, and `pickStatus` are retained. Optional
new fields are:

| Field | Meaning |
| --- | --- |
| `dateTimeOriginalISO8601` | Capture timestamp as supplied by Lightroom; no timezone is invented |
| `cameraMake`, `cameraModel` | Camera manufacturer and model |
| `lens` | Lens display name |
| `shutterSpeed`, `aperture`, `isoSpeedRating`, `focalLength` | Lightroom's displayed exposure values |
| `dimensions`, `croppedDimensions` | Original and cropped pixel dimensions, as display strings |
| `fileType` | Displayed file format |

Exposure/dimension display strings may reflect the Lightroom locale and are not
promised as machine-parsable numeric values. Missing, unsupported or failed
optional SDK fields are omitted; scans and photos without EXIF still transfer.
The projection is explicit. No GPS, file paths, camera serial numbers, personal
contact fields, keywords, or full metadata tables are read or transmitted.

`SelectionMetadata.lua` is shared by transfer and the local preview. The preview
shows the first five selected photos and does not transmit data. Text is bounded
and UTF-8-safe; JSON escaping preserves quotation marks and backslashes.

See Adobe's [LrPhoto SDK reference](https://lrc.mcor.dev/modules/LrPhoto.html)
(Adobe-authored reference hosted as a mirror) for the raw and formatted getters.
`LrTasks.pcall` handles optional SDK failures without the ordinary Lua `pcall`
yield restriction that previously affected this plugin.

## Larger selections and compatibility

The new protocol accepts **1–50 selected photos** and at most **64 KiB of encoded
JSON**. Invalid or oversized requests leave the previous snapshot unchanged.
Data still expires after five minutes; nothing is written to the Lightroom catalog.

Authenticated `GET /v1/capabilities` advertises protocol version 2, the permitted
fields, limits, and `catalog_writes: false`. The new sender checks this endpoint
when extended metadata or more than five photos are requested. If an older bridge
returns 404, up to five photos can still transfer with only the three original
fields; the helper response reports `metadata_mode: basic_legacy_bridge`.
A larger selection is refused with a clear upgrade error; it is never truncated.
Authorization errors, timeouts and invalid capability responses do not trigger
an insecure fallback. Only a genuine 404 permits the legacy projection.

The new plugin and new Python helper must be used together. A legacy helper does
not know the extra fields. This PR does not activate or switch the running stack.
A scheduled Mac integration check is still required before deployment.

## Status

`connection_status` exposes whether the bridge is running and whether a selection
is `none`, `received`, or `expired`, including count, age and remaining lifetime.
`connected: false` remains a truthful statement that no continuous live catalog
connection exists; the accompanying message explains the snapshot state.
The metadata tool preserves `reason: no_selection` for an empty/expired snapshot.
Older bridge status responses remain readable. No new write tool is introduced.

## Verification

Tests execute the actual projection/JSON module using Lua 5.1 with mocked SDK
getters. They cover missing EXIF, getter failures, Unicode boundaries and privacy
allowlisting. HTTP integration tests cover helper → bridge → MCP, 50 photos,
legacy fallback, rejected requests, authentication and snapshot expiration.
These tests do not substitute for executing the plugin inside Lightroom Classic.
