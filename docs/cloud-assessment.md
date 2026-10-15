# Phase 4: Lightroom (cloud) API assessment

**Result: not built. Separate adapter later, only if a concrete need exists.**

Findings (from Adobe's public docs; re-check before relying on them):
- Cloud access is via the *Lightroom Services* API: an integration is created in the Adobe
  Developer Console, scopes `lr_partner_apis` / `lr_partner_rendition_apis`, API key plus
  OAuth for each customer ([Creating an Integration](https://developer.adobe.com/lightroom/lightroom-api-docs/getting-started/create-integration),
  [OAuth Integration](https://developer.adobe.com/lightroom/lightroom-api-docs/getting-started/authenticate-customers)).
- The API is designed for **partner apps** (upload and render customer content), not for
  controlling a personal catalog. Refresh-token renewal needs extra setup with Adobe, and
  the older API terms restrict use to the registered integration
  ([lightroom-partner-apis](https://github.com/AdobeDocs/lightroom-partner-apis)).
- It addresses **Lightroom (cloud)** catalogs, a different product than Lightroom Classic.
  Classic photos only appear there if synced, so this does not replace the Classic bridge.

If pursued, it would be a third `CatalogAdapter` behind the same permission gate. Open
questions: eligibility of a personal/open-source integration, data residency, terms.
