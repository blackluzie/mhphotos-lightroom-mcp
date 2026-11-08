# Workflows

All steps are tool calls from an MCP client such as Claude. Public publishing is always
two-step: a dry run you read first, then the same call with `confirm=true`.

## 1. "All 5-star photos from 2026 onto mhphotos.de, with category and description"
1. `list_photos(min_rating=5, year=2026)` finds them in Lightroom (page through with `offset`).
2. `send_to_mhphotos_inbox(photo_ids, confirm=true)` exports and uploads them to the
   MHPhotos Foto-Inbox (status `wartet`, still not public). Returns the inbox ids.
3. `mhphotos_analyze_inbox(item_ids)` makes the MHPhotos app generate title, description,
   alt text and category suggestions in the background.
4. After about a minute, `mhphotos_inbox(status="wartet")` shows the suggestions. Check them,
   and look up valid categories with `mhphotos_galleries` if you want to change any.
5. `mhphotos_publish(item_ids)` shows exactly what will go live (title, description,
   categories). Then `mhphotos_publish(item_ids, confirm=true)` publishes. Per-item
   corrections go into `overrides`.

Needs: `MHPHOTOS_INBOX_URL`, `MHPHOTOS_ADMIN_TOKEN`, `MHPHOTOS_EXPORT_DIR`, and for step 5
`MHPHOTOS_ALLOW_PUBLISH=1`.

## 3. "Best 5 South Tyrol photos of album 09 2026, suggest edits with my presets or a new preset"
1. `list_collections(name_contains="09 2026")` finds the album.
2. `list_photos(collection_id=…, location="Südtirol")` (or `near_lat=46.7, near_lon=11.4,
   radius_km=80` if place names are missing; add `min_rating=3` for big albums).
3. `get_photo_previews` shows the candidates 12 at a time. Claude judges composition, light
   and sharpness and picks the best 5 with reasons. Large albums: narrow down first, every
   image costs tokens.
4. For the picks: `get_develop_settings` (what is applied now), `list_presets` (what you
   own), then a proposal per photo: an existing preset, or concrete settings such as
   `{"Exposure2012": 0.3, "Highlights2012": -40, "Vibrance": 15}`.
5. Only after you agree: `apply_preset` / `apply_develop_settings` with `confirm=true`, or
   `create_preset(name, settings, confirm=true)` to keep a new look as a reusable preset
   (it appears under the plugin's own preset folder). Dry runs show old and new values
   first. Every change is one history step in Lightroom's Develop module, so it can be undone.

Needs `MHPHOTOS_ALLOW_WRITES=1` for steps 5 only; looking and proposing needs no opt-in.
Develop parameters are a fixed whitelist with Lightroom's ranges (tone, presence, white
balance, sharpening/noise, vignette, grain, HSL, grayscale); anything else is rejected.

## 2. "My 2026 Instagram photos: find them in Lightroom and publish them on MHPhotos"
1. `mhphotos_instagram_posts(year=2026)` lists your Instagram posts not yet on the MHPhotos
   site (the library is synced inside the MHPhotos app; the result shows `newest_sync`).
2. `mhphotos_instagram_images(post_ids)` shows up to 6 posts at a time as images.
3. Shortlist the Lightroom originals: `list_photos` with `captured_from`/`captured_to` in the
   weeks **before** the post date (a post comes after the shot), plus `min_rating`,
   `location` or `keyword` if you know them. Look at them with `get_photo_previews`.
4. Claude compares subject and composition and names the match per post, or says "no
   match". Instagram crops (often 4:5) and recompresses, so this is a visual judgement,
   not a pixel comparison. Confirm uncertain matches yourself.
5. Optional bookkeeping: `add_keywords(photo_ids, ["Instagram"], confirm=true)` marks the
   matched originals in Lightroom, so later searches can use `keyword="Instagram"`.
6. Then steps 2 to 5 of workflow 1 (inbox, AI suggestions, `mhphotos_publish`).

Instagram image links are temporary. If a link has expired the tool says so; sync the
Instagram library in the MHPhotos app and try again. Only https addresses on Instagram/Meta
CDN hosts are downloaded (no redirects elsewhere, max 6 MB), because the links come from
stored data and are treated as untrusted.

## 4. Keep a pick: collection, color label, title
After "best 5 of album 09 2026" the result should not live only in the chat:
1. `create_collection("Südtirol 09 2026 Auswahl", confirm=true)`, then
   `add_to_collection(collection_id, photo_ids, confirm=true)`.
2. Optionally `set_color_label(photo_ids, "green", confirm=true)` and `set_title_caption(...)` (existing text
   is only replaced with `overwrite=true`). Each of these can be undone with `undo_last_change`.

## 5. Try an edit safely, compare, repeat
1. `create_virtual_copies([photo_id], confirm=true)` gives a copy id; the original stays untouched.
   With `state: running` Lightroom is still at it: ask `virtual_copy_job(job_id)` every 30-60 s until it is done; for a few
   minutes new copies may answer `photo_busy` (just retry later).
   If it answers `virtual_copy_timeout` or `copy_pending`, make the copy by hand in Lightroom (Cmd+'), find it with
   `get_photo_metadata`/`list_photos` and go on with that id (`crop_to_aspect(..., virtual_copies=false)` for a crop).
2. `apply_preset` or `apply_develop_settings` on the **copy**, then `get_photo_previews([original, copy])`.
3. Not happy? Adjust values and apply again (each is a history step in Lightroom). Happy? Keep the copy.
4. For a series: `sync_develop_settings(from_photo_id, to_photo_ids)` copies the look (without white balance by
   default, since that differs per photo).

## 6. Browse a big album cheaply
`list_photos(collection_id=..., min_rating=3)`, then `get_contact_sheet(ids)` shows up to 30 photos as one
numbered image. Look closely only at the few picks with `get_photo_previews`.

## 7. Plan Instagram posts
`mhphotos_schedule_instagram(caption, scheduled_at, photo_id=...)` (a Lightroom photo) or `inbox_item_id=...` (an
MHPhotos inbox item, no Lightroom needed) shrinks the photo for Instagram and puts it into the **social queue** of
the MHPhotos app, the same queue its "Neues Foto" flow uses. By default it waits there as "wartet auf Freigabe" until you
approve it in *Admin › Social › Warteschlange*; then **MHPhotos publishes it automatically at that time**
(`needs_approval=false` skips the approval). `mentions=[...]` tags trusted accounts in the picture, `location_name` sets the place.
Accounts default to `mhphotos`; `malou` and `martin` only when named; `post_type` is `post` or `story`. MHPhotos
duplicate check runs first. `adjust={...}` applies basic edits to the Instagram copy only (preview them with
`mhphotos_edit_preview`). Check with `mhphotos_instagram_queue`, cancel in time with `mhphotos_cancel_instagram`.
The time must be 15 minutes to 60 days ahead, and you are asked directly before anything is queued.

There is no separate Social Hub site any more: its screens live in the MHPhotos admin (`/admin/social/queue`). The
routes keep the old `social-hub-*` name (MHPhotos forwards them to the publishing worker), and this server uses only
those; it does not talk to the retired `insta-queue` routes or to any other host.

## 8. What is still open for 2026?
`send_to_mhphotos_inbox(..., mark_in_lightroom=true)` adds the keyword `MHPhotos-Inbox`; matching an Instagram post
(workflow 2, step 5) adds `Instagram`. `backlog_report(2026)` then lists the good photos that carry neither marker.

## 9. Without Lightroom: work from the MHPhotos inbox (travel)
Photos that reach the inbox (Lightroom Classic plugin, the Mac sync service, Lightroom Mobile exports) can be handled
without Lightroom: `mhphotos_inbox` lists them, `mhphotos_inbox_contact_sheet` and `mhphotos_inbox_images` show them,
`mhphotos_analyze_inbox` adds AI titles and categories, `mhphotos_edit_preview` shows a basic edit before/after,
`mhphotos_publish` puts chosen items on the site and `mhphotos_schedule_instagram(inbox_item_id=...)` queues them for
Instagram. Full guide for the cruise, including what works and what does not: [kreuzfahrt.md](kreuzfahrt.md).

## 10. Instagram posts without a homepage photo, from a claude.ai chat
Needs the connector with `rebuild-mac.sh --mhphotos-url https://<your MHPhotos domain>` (admin token in the Keychain
entry `mhphotos-admin-token`) and the n8n inbox workflow.
1. `mhphotos_instagram_posts(year=…)` lists posts that are not on the site yet; `in_inbox` shows posts whose
   original already waits in the inbox (skip those).
2. `mhphotos_instagram_images(post_ids)` shows up to 6 posts; `list_photos(captured_from/captured_to in the weeks
   before the post)` and `get_contact_sheet` / `get_photo_previews` find the Lightroom original.
3. Compare: same subject and framing = match. If the current Lightroom version looks clearly different from the post
   (other crop, colours, re-edit), say so; the post shows the version that was published then.
4. `export_photos([photo_id], confirm=true, instagram_post_id=post_id)` exports it. n8n uploads it with
   `instagramId`; when you take it over in the MHPhotos inbox, the app links the Instagram post to the new homepage
   photo, so it disappears from step 1.
5. Optional (with `--allow-writes`): `add_keywords([photo_id], ["Instagram"], confirm=true)` marks the original.

## 11. Catalog check: photos in the wrong date folder, duplicates
Folders are `JJJJ/JJJJ-MM/JJJJ-MM-TT`; the capture time decides where a photo belongs. Plan: `docs/plan-katalog-pruefung.md`.
1. `catalog_audit_start(year=2020)` for a first test, later without `year` for the whole catalog. It runs in the
   background on the Mac and changes nothing; `catalog_audit_status()` shows progress and, at the end, the counts. With `year` the run first scans the
   whole catalog by capture year (percent shown), then reads details only for that year. `catalog_audit_cancel()`
   stops a run at its next step.
   Target structure: `JJJJ-JJJJ/JJJJ/JJJJ-MM/JJJJ-MM-TT` (decade, year, month, day). `wrong_folder` has levels
   `year`, `month`, `day`, `decade` and `no_day` (no day folder); `video_no_date`, `date_in_name` and
   `near_duplicates` are their own kinds. Photos whose file name carries the date (`date_in_name`) get their
   capture time with `write_name_dates_to_files`: it writes the date into the JPEG (exiftool, backup first, one photo
   by default, opt-in `--allow-file-edits`). Then in Lightroom select the collection "Prüfen › Sonstiges › Datum in Datei
   geschrieben" and use *Metadata > Read Metadata from Files*; run the check again so they show up as `wrong_folder`.
2. `catalog_audit_results(kind="wrong_folder", year=…, level="year"|"month"|"day")`: `found` is the folder it is in,
   `expected` the day folder it belongs in. `day_diff` ±1 is often a time-zone or midnight effect (camera clock on
   travel), so check those before moving. Other kinds: `no_date`, `implausible_date`, `unrecognised_folder`,
   `missing_file` (only with `check_files=true`), `duplicates`, `possible_duplicates`, `non_photo` (screenshots,
   messenger pictures, downloads: no camera plus other signs, or a screenshot file name), `jpeg_with_raw` (the
   camera's JPEG next to its RAW; only the RAW is needed) and `reimported_export` (a JPEG export of a RAW that came
   back into the catalog).
3. Duplicates: candidates share capture second, camera, pixel size, file type and byte size; a fingerprint of the
   first and last MiB confirms them. `keep` is the copy with the most work in it (edits, stars, pick, keywords,
   label, virtual copies, right folder). `possible_duplicates` (same shot, same file name with a copy suffix,
   different bytes) need a look with `get_contact_sheet`. Virtual copies and RAW+JPEG pairs never count.
4. With `--allow-writes`: `catalog_audit_collections(kind, year, confirm=true)` builds review collections under
   *Prüfen* (e.g. *Prüfen › Ordner falsch › → 2021-07-14*, *Prüfen › Duplikate › entfernen*). The Lightroom SDK
   cannot move or remove photos: drag each collection's photos onto the target folder in the Folders panel, and
   remove duplicates from the *entfernen* collection yourself. Never move files in Finder or on the NAS directly.
5. Reading the files for fingerprints needs macOS access to network volumes for the service; if
   `duplicate_files_unreadable` shows up in the summary, allow it in System Settings → Privacy & Security.
6. The summary of a finished check has `storage`: GB per capture year on the NAS and `freeable_gb` (duplicates,
   JPEGs next to their RAW, re-imported exports, non-photos), e.g. "removing these frees 380 GB".

## 12. Sort a new shoot (after an import)
1. `photos_by_date(captured_from, captured_to, unreviewed=true, refresh=true)`: everything without stars or flag
   (`refresh` once after an import, so the new photos are in the date index).
2. `get_contact_sheet` in batches of 20–30: reject clear misses (blurred, closed eyes, test shots, near-identical
   frames of a burst except the best) with `set_flag(…, "rejected")`, rate keepers with `set_rating` (3 = good,
   4 = very good, 5 = portfolio). Always dry run first and show the user the list; the user decides, nothing is
   deleted.

## 13. Keywords, titles and captions for the best photos
1. `photos_by_date(…, min_rating=4, missing="caption")` (or `missing="keywords"`).
2. `get_photo_previews` (up to 6 at a time), then per photo: 5–12 German keywords (subject, place, season, mood;
   no hashtags), a short title and one or two sentences of caption, factual and in the user's voice (look at a few
   captions from `mhphotos_instagram_posts` first).
3. `add_keywords` and `set_title_caption`, dry run first. Both are undoable with `undo_last_change`.

## 14. Best of a year or a trip
1. `photos_by_date(captured_from, captured_to, min_rating=4)`; `get_contact_sheet` over the candidates.
2. Pick 20–40 with variety (subjects, places, light), avoid near-duplicates; suggest the order.
3. `create_collection("Best of 2025")` and `add_to_collection`, then `export_photos` in batches of 12: n8n puts them
   into the MHPhotos inbox, where they become a gallery.

## 15. Instagram week plan
Needs `rebuild-mac.sh --mhphotos-url … --allow-instagram`; posts go into the MHPhotos social queue and are
published there automatically at the planned time.
1. `instagram_candidates(account, count=9)`: good Lightroom photos (4+ stars, not rejected, no keyword
   "Instagram") that the MHPhotos duplicate check finds neither planned nor posted.
2. `get_photo_previews` / `get_contact_sheet`: pick three for the week with variety; `mhphotos_instagram_queue`
   shows what is already planned, `mhphotos_best_times(account)` the best hours.
3. Captions in the style of earlier posts (`mhphotos_instagram_posts`): German, short, place, a few hashtags.
4. Per post `mhphotos_schedule_instagram(caption, scheduled_at, photo_id=…)` **without** confirm: the user sees the
   plan (photo, time, text, duplicate check). Only after the user's OK the same call with `confirm=true`.
   Cancel later with `mhphotos_cancel_instagram`. After posting, `add_keywords([photo_id], ["Instagram"])`.
5. Carousel: `mhphotos_schedule_instagram(caption, scheduled_at, photo_ids=[…2–10 Lightroom ids in order…])`, same
   dry run first; the duplicate check looks at the first image.

## 16. Places for photos without GPS
1. `suggest_locations(captured_from, captured_to)`: for camera photos without GPS, the position of the nearest
   phone photo with GPS (within 30 minutes), grouped by position.
2. Check one pair first (`get_photo_metadata`): if the camera clock differs from the phone (home time abroad),
   repeat with `clock_offset_minutes`.
3. `set_location(photo_ids, latitude, longitude, city=…, country=…)` per group, dry run first. The result says
   whether this Lightroom accepted the GPS write (`gps_written`); place names are written either way.

## 17. Reels (silent photo slideshow)
Needs `--allow-instagram` and the reel renderer on the Hetzner host (`apps-platform/infra/mhphotos-reel-renderer`).
1. Reels are made from homepage photos: `mhphotos_site_photos(query)` (for Lightroom photos: best-of → inbox →
   take over first, see 14).
2. Pick 6–12 with a story and good order (opening shot, variety, strong last image).
3. `mhphotos_create_reel(photo_ids, title, caption)`, dry run first. The renderer makes a 1080×1920 video
   (3.2 s per photo, slow zoom, cross-fades, no music) within minutes; `mhphotos_reel_jobs` shows `bereit`.
4. The user approves in the MHPhotos Content Studio. An automatically published reel cannot get Instagram music
   later: for music, download the MP4 there and post it in the Instagram app.

## 18. Lightroom collection straight into the inbox (no chat needed)
1. In Lightroom: *Library > Plug-in Extras > MHPhotos MCP: Auto-Export einrichten*, collection e.g. "→ MHPhotos",
   minimum rating, first with dry run, then active.
2. On the Mac once: `./scripts/rebuild-mac.sh --hot-folder --apply --start`. macOS may ask whether Python may access
   the Pictures folder: allow it.
3. Drag photos into the collection. Within about 15 minutes the plugin exports them, the HTTP service checks and
   batches them (bad files go to `~/Pictures/MHPhotos-Inbox/fehler/`), n8n uploads them, and they wait in the inbox.


## 19. Instagram formats (4:5 feed, 9:16 stories) as virtual copies
1. Pick the photo, look at it (`get_photo_previews`) and choose the subject: where is it (focus_x / focus_y, 0 = left/top, 1 = right/bottom)?
2. `crop_to_aspect(photo_ids, aspect="4:5", focus_x, focus_y)`, dry run first: each photo gets a virtual copy "Insta 4:5"
   that is cropped; the original stays as it is. `aspect="9:16"` makes "Story 9:16" copies. At most 10 photos per call,
   one call per focus point.
3. Check the copy with `get_photo_previews`; a different crop is another call with a new focus (delete the old copy in Lightroom).
   Tested in Lightroom (plugin 0.1.14): 4:5, 9:16 and straightening give the exact format. The result lists `used` with the
   size Lightroom reports after the crop, so a wrong format shows at once. Lightroom reads the crop in the stored frame
   (phone portraits are stored landscape); the tool converts that itself and checks the result.
   Straightening: `crop_to_aspect(photo_ids, aspect="original", angle=-2.5, virtual_copies=false)`. A negative angle levels a
   horizon that rises to the right; read the tilt from the preview (rise in pixels over the width, about atan(rise / width)) and
   adjust in steps of 0.5. The crop is centred and as large as fits without empty corners (focus is ignored).
   Copies are created in about a minute; if the call times out, the copy usually exists anyway: drag it into a
   collection and find it there by its file name (a search over the whole catalog is too slow).
4. Virtual copies can also get a look: `apply_preset` or `apply_develop_settings` (for black and white: `ConvertToGrayscale`).

## 20. Catalog overview per year and the move list at folder level (plugin 0.1.26)
1. `catalog_audit_start()` without year: the whole catalog (several minutes). Since 0.1.26 the run also keeps a compact
   snapshot of every photo on the Mac (`~/.mhphotos-lightroom-mcp/audit/snapshot.json.gz`: capture time, stars, flag,
   keywords count, size, camera, lens, focal length, GPS, folder). Archive candidates, gear statistics, spots and the
   pre-sorting backlog are computed from it, without asking Lightroom again.
2. `catalog_audit_overview()`: one row per year with photos, GB and the open points (wrong folder, duplicates to remove,
   non-photos, JPEG next to RAW, no date …), plus when each scope was last checked (`history`).
3. `catalog_folder_moves(year=…, level=…)`: wrong folders grouped by the folder they are in. `whole_folder=true` = the whole
   folder belongs into one target: drag it in Lightroom's Folders panel (or rename it) in one step. The rest still goes
   through `catalog_audit_collections(kind="wrong_folder")`.

## 21. Archive candidates (free space)
1. `archive_candidates(min_age_years=5)`: untouched photos (no stars, flag, keywords, edits, colour label, virtual copies)
   older than 5 years, per year and per folder (biggest first, `whole_folder`), plus rejected photos of any age.
2. `archive_candidates_collections(confirm=true)` puts them into "Archiv-Kandidaten › <year> › unberührt"
   (`include_rejected=true`: also "Abgelehnt › alle Jahre"). Moving to an archive drive is done in Lightroom
   (Folders panel: drag the folder onto the archive volume), never in Finder.

## 22. Pre-sorting at night
Claude looks at the photos, the server only prepares and records. Nothing changes stars, flags or files.
1. `presort_status()`: months done, the month in progress, the next months with unreviewed photos (no stars, no flag).
2. `presort_month("2024-05")`: unreviewed photos of the month in series (same camera, ≤ 5 s apart) and contact-sheet
   batches of 6 (a series stays together).
3. Per batch: `get_contact_sheet(batch.photo_ids, columns=6, cell=200)`, judge each photo with the owner's rules (5★ = could
   go online, family at most 4★, per series only the best), then
   `presort_apply(month, top=[…], good=[…], reject=[…], series_rest=[…], batch=N, confirm=true)`.
4. Last batch: `finished=true`. The owner sees "Vorsortieren › 2024-05 › Kandidat 5★ / Kandidat 4★ / Aussortieren
   (Vorschlag) / Serie – Rest (Vorschlag)" in Lightroom and decides there (stars, removing).
A nightly run is a scheduled Claude task with the MHPhotos Lightroom connector (writes allowed): "Run /mhphotos-vorsortieren:
take presort_status().next[0] and work through up to 40 batches". Lightroom must be running on the Mac.

## 23. Keyword clean-up
1. `keyword_check_start()`, then `keyword_check_status()` until done (photo counts per keyword take a while).
2. `keyword_check_results("duplicates")`: same name at the same place, or a loose keyword next to the same one inside a
   hierarchy (keep + merge). `merge_keywords(source_ids=[merge ids], target_id=keep id, confirm=true)` moves the photos.
   Afterwards in Lightroom: *Metadata > Purge Unused Keywords*.
3. `variants` (singular/plural, spelling) only after a look; `same_name_elsewhere` is usually right (two towns of the same
   name); `unused` goes with the purge; `loose` lists top-level keywords with photos to drag into a hierarchy.

## 24. Mark in Lightroom what is online
`mark_online_in_lightroom()` (dry run), then `confirm=true`: photos on mhphotos.de get the keyword "MHPhotos-Online", photos of
Instagram posts "Instagram", and the smart collections "MHPhotos › Online auf mhphotos.de" / "MHPhotos › Auf Instagram" fill
themselves. Matching uses the Lightroom id (uploads since 0.1.26), else capture time + file name or camera. Safe to repeat
(e.g. weekly); `backlog_report` and `instagram_candidates` then skip what is already online.

## 25. Own photos at the MHPhotos spots
1. `spot_photos(radius_m=300, min_rating=4)`: per spot the own photos nearby (best ones with ids) and the spots without a
   good own photo (ideas for the photo planner).
2. `spot_photos_collections(spot_ids, confirm=true)`: "MHPhotos-Spots › Spots › <name>" with the good photos, e.g. to send the
   best one to the inbox for the spot page.
3. `mhphotos_sync_spot_photos(confirm=true)`: the counts per spot go to the MHPhotos admin (no images, nothing public).

## 26. Gear statistics
`gear_usage(year=None)`: photos per camera and lens (rated 4+, 5★, first/last use, focal lengths), matched to the Ausrüstung
list of mhphotos.de (unmatched EXIF names, active gear without photos). `mhphotos_sync_gear_usage(confirm=true)` shows the
numbers on the Ausrüstung admin page.

## 27. Trip → blog draft
1. `trip_outline("2026-10-19", "2026-11-02", collection_id=<"Kreuzfahrt 2026">, title="Kreuzfahrt 2026")`: per day places,
   km from GPS, map link, best photos, and a Markdown draft with placeholders.
2. Write the text with the owner (what happened each day), send the best photos to the inbox (`send_to_mhphotos_inbox`).
3. `mhphotos_create_blog_draft(title, content=…, tags=[…], confirm=true)` saves it unpublished in Admin › Blog.

## 28. Double imports per folder
After a catalog check over the whole catalog, `duplicate_folders()` lists the verified duplicates per folder pair (copy
folder → folder with the original), biggest first. `whole_folder=true` means the copy folder holds nothing else: look at it
once in Lightroom and remove the folder there. Review collections for the single copies:
`catalog_audit_collections(kind="duplicates")`.

## 29. Missing and offline files
`catalog_audit_start(check_files=true)`, then `missing_files_by_drive()`: per drive `offline` (not connected, nothing lost:
connect it) or `missing` (connected, file gone or moved outside Lightroom: *Library > Find Missing Photos*), with the folders
and whether a whole folder is affected.

## 30. Close GPS gaps
1. `gps_gaps(captured_from, captured_to, window_minutes=30)`: photos without GPS taken close to a photo with GPS (often the
   phone), with the minutes between them; `by_month` shows where most gaps are.
2. Look at a few samples, then `fill_gps_gaps(captured_from, captured_to, confirm=true)` (at most 5000 photos per call).
   Only photos without GPS are written; place names stay as they are (`suggest_locations` for those).

## 31. Print candidates (wall art)
`print_candidates(min_rating=4, min_size="45×30")`: rated photos with enough pixels after cropping, grouped by the biggest
size (30×20 … 120×80 cm at 150 dpi). `print_candidates_collections(confirm=true)` fills "Druck-Kandidaten › bis <size> cm ›
5★/4★". Needs a catalog snapshot from plug-in 0.1.27+ (image size after cropping).

## 32. Photo calendar
1. `calendar_candidates(year=2027, per_month=8, orientation="landscape", min_size="45×30")`: per month the best photos taken
   in that month (any year), one per day, spread over years and places; `months_short` names months with too few.
2. Look at them with `get_contact_sheet` (6 per sheet) together with the owner.
3. `calendar_collections(year=2027, confirm=true)` fills "Kalender 2027 › Vorschläge › 01 Januar … 12 Dezember", or with the
   final pick `photo_ids_by_month={"1": [...], ...}`. The final photo per month is chosen in Lightroom.

## 33. Weekly report
`weekly_report(days=7)`: new photos (and how many are unreviewed), the unreviewed backlog with the next months for
pre-sorting, what went online on mhphotos.de/Instagram, inbox items waiting, spots without a good own photo. Meant for a
scheduled run every Monday; each part that cannot be read says why.
