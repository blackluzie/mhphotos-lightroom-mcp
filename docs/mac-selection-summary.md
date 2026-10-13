# Read-only photo selection summary

`selected_photo_summary()` summarizes only the most recently explicitly submitted,
unexpired photo selection (at most 50 photos). It returns photo count, grouped
camera make/model, lens, file type, rating and pick-status values, and counts of
available metadata fields. No filenames or additional Lightroom fields are read.

Each group contains `values` (display value and count) and `missing_count`.
Missing, empty and whitespace-only values are counted as missing. Numeric zero
is a present value, not missing. Integer/string representations of the same rating
or flag are grouped together. Unknown display values remain unchanged; this tool
does not infer meanings, numeric exposure values, timezone conversions or changes.
Values are sorted deterministically. Legacy snapshots lacking EXIF remain usable.

The tool validates the existing bounded metadata projection before aggregation.
Malformed, empty or unavailable selections return `photo_count: 0` and a reason;
expired data is never presented as a successful summary. It adds no persistence,
scans, automatic transfer or snapshot lifetime extension.

Stdio and HTTP expose the same read-only function with existing `lightroom:read`
scope. Ratings and flags are only counted; no write operations exist. No Lightroom
plugin reload is needed for the MCP-only change in the separate Mac test package.
