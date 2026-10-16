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
2. `apply_preset` or `apply_develop_settings` on the **copy**, then `get_photo_previews([original, copy])`.
3. Not happy? Adjust values and apply again (each is a history step in Lightroom). Happy? Keep the copy.
4. For a series: `sync_develop_settings(from_photo_id, to_photo_ids)` copies the look (without white balance by
   default, since that differs per photo).

## 6. Browse a big album cheaply
`list_photos(collection_id=..., min_rating=3)`, then `get_contact_sheet(ids)` shows up to 30 photos as one
numbered image. Look closely only at the few picks with `get_photo_previews`.

## 7. Plan Instagram posts
`mhphotos_schedule_instagram(photo_id, caption, scheduled_at, confirm=true)` exports the photo, resizes it for
Instagram and puts it into the MHPhotos Instagram queue. **The MHPhotos app publishes it automatically at that
time**; check with `mhphotos_instagram_queue` and cancel in time with `mhphotos_cancel_instagram`. The time must
be 15 minutes to 60 days ahead, and you are asked directly before anything is queued.

## 8. What is still open for 2026?
`send_to_mhphotos_inbox(..., mark_in_lightroom=true)` adds the keyword `MHPhotos-Inbox`; matching an Instagram post
(workflow 2, step 5) adds `Instagram`. `backlog_report(2026)` then lists the good photos that carry neither marker.
