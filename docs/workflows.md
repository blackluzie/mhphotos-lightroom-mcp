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
Done today:
- `mhphotos_instagram_posts(year=2026)` lists your Instagram posts that are not yet linked
  to a photo on the MHPhotos site (the library is synced inside the MHPhotos app; the
  result shows `newest_sync`).

**Not automated yet: matching a post to its Lightroom original.** Instagram crops and
recompresses, and a post date is not the capture date, so file names and dates do not
identify the photo. Options, in the order I would build them:
1. *Visual matching by Claude:* `get_photo_previews` now exists, so Claude can compare a
   Lightroom preview with the Instagram thumbnail for a shortlist (rating and year as
   pre-filter). Reliable; the missing piece is only a tool that shows the Instagram image.
2. *Perceptual hashes:* fully automatic, but fragile with Instagram crops; would need an
   image library and still needs a confirmation step.
3. *Marker in Lightroom:* tag posted photos with a keyword such as `instagram-2026`
   and use `list_photos(keyword=...)`. Zero guessing, but needs your discipline.

After a match is found, steps 2 to 5 of workflow 1 apply.
