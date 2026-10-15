# Individual candidate previews

The private stdio tool `lightroom_collection_candidate_preview` reads one JPEG
preview selected from a numbered contact sheet. Pass the returned top-level
`collection_path` and `session_id` together with the chosen label's zero-based
`collection_offset` and `metadata_fingerprint`. The fingerprint is a deterministic
comparison of bounded metadata, collection path, position and catalog session;
it is neither a secret nor an authorization token.

The tool performs one existing authorized `collection_photos` read, with limit
one and previews enabled. If current metadata differ, it discards both the image
and unexpected photo metadata and returns `photo_metadata_changed`; obtain a fresh
sheet. Invalid references are rejected before fetching. Removed photos, changed
catalog sessions and unavailable previews retain explicit failure responses.
A successful comparison adds `candidate_verification.metadata_match: true`.
This can also accompany an unavailable preview: metadata matching alone does not
mean an image was delivered.

This checks metadata, not stable photo identity or pixels. Two distinct photos
can have identical metadata, and ordering or membership can change between reads.
It must never be used as a write handle or authorization for editing. The image
uses the existing bounded JPEG preview, not the original or full resolution.
No new Lightroom operation, metadata access, automatic retry or permission is
introduced. Ratings, photos, presets and catalog settings are never changed.
The tool is excluded from the public HTTP/OAuth registration.

Tests cover context binding, canonical metadata, input validation before fetching,
metadata mismatches without image or unexpected metadata disclosure, native MCP
image delivery, missing previews/photos, catalog errors, contact-sheet round trips
and the limitation for identical metadata.
