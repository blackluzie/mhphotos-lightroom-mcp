# Filter the submitted photo selection

`selected_photo_filter(file_name=None, camera_model=None, lens=None,
file_type=None, rating=None, pick_status=None)` returns matching records from
only the explicitly submitted, unexpired snapshot (maximum 50 photos).
No catalog enumeration, file access or new Lightroom fields are involved.

At least one filter is required. Filters combine with AND. File name, camera
model and lens use literal, Unicode-aware case-insensitive substring matching.
File type matches exact text case-insensitively. Text filters are trimmed and
must be nonempty. Each input is at most 80 characters and rejects control characters.
Regex-like characters are literal. Names are never commands.

Rating and pick status are exact submitted-value strings. Integer/string values
compare alike, e.g. `rating="5"` or `pick_status="0"`. No meaning is inferred
for unusual display values. An empty string matches an explicitly empty field;
it does not match an absent field or a numeric zero. Nothing changes ratings or
flags, even when these are used as filters.

Results contain `photos`, `photo_count`, `snapshot_count`, `applied_filters`,
`source` and `catalog_writes: false`. Matching records retain their existing
metadata and order, including duplicate names. Zero matches is a valid result;
an expired/unavailable/malformed snapshot instead has a reason. The tool adds
no persistence, automatic transfer or lifetime extension.

Stdio and HTTP retain read-only annotations and the existing `lightroom:read`
scope. Only the separate test package is updated; no Lightroom reload is needed.
