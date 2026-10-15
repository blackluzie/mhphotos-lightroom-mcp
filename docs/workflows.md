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

## 2. "My 2026 Instagram photos: find them in Lightroom and publish them on MHPhotos"
Done today:
- `mhphotos_instagram_posts(year=2026)` lists your Instagram posts that are not yet linked
  to a photo on the MHPhotos site (the library is synced inside the MHPhotos app; the
  result shows `newest_sync`).

**Not automated yet: matching a post to its Lightroom original.** Instagram crops and
recompresses, and a post date is not the capture date, so file names and dates do not
identify the photo. Options, in the order I would build them:
1. *Visual matching by Claude:* a tool that returns a small Lightroom preview next to the
   Instagram thumbnail so Claude can judge "same photo?" for a shortlist (rating and
   year as pre-filter). Reliable, needs a preview command in the plugin.
2. *Perceptual hashes:* fully automatic, but fragile with Instagram crops; would need an
   image library and still needs a confirmation step.
3. *Marker in Lightroom:* tag posted photos with a keyword such as `instagram-2026`
   and use `list_photos(keyword=...)`. Zero guessing, but needs your discipline.

After a match is found, steps 2 to 5 of workflow 1 apply.
