---
name: mhphotos
description: Foto-Durchsicht mit dem MHPhotos-Lightroom-Connector wie bei „Kreuzfahrt 2026“, z. B. /mhphotos Oktober 2026; mit /mhphotos Instawoche Kreuzfahrt 2026 Posts, Stories, Reel und mhphotos.de mit Titel, Kategorie, SEO.
---

# /mhphotos: eine Sammlung komplett durchgehen (wie „Kreuzfahrt 2026“)

Argument: eine Sammlung („Kreuzfahrt 2026“), ein Monat („Oktober 2026“, „2026/10“), ein Jahr („2026“) oder ein Ordner.
Ohne Argument zuerst fragen, was gemeint ist. Antworte auf Deutsch und duze den Eigentümer (Martin).
Nichts wird gelöscht, verschoben oder veröffentlicht; das entscheidet der Eigentümer.

Ablauf in dieser Reihenfolge, jede Phase mit kurzem Bericht:
**0 Vorbereitung → 1 Durchsicht → 2 Sterne und Stichwörter → 3 Instagram-Zuschnitte → 4 Aussortieren in Bögen → 5 Fotobuch und Abschluss.**
Mit „Instawoche“ im Aufruf (z. B. „/mhphotos Instawoche Kreuzfahrt 2026“) gleich Abschnitt 6.
Wird ein Lauf in einem neuen Chat fortgesetzt, zuerst fragen bzw. aus dem letzten Bericht übernehmen, bei welcher Phase
und welchem Bogen es weitergeht, und dort einsteigen.

## 0. Vorbereitung
1. `connection_status`. Sind die Lightroom-Tools gar nicht da oder meldet der Connector eine abgelaufene Anmeldung:
   den Eigentümer bitten, „MHPhotos Lightroom“ in claude.ai unter Einstellungen → Connectors neu zu verbinden.
   Ist die Bridge getrennt: in Lightroom das Plugin neu laden (Zusatzmodul-Manager → „Neu laden“).
   Es kann immer nur **ein** Befehl gleichzeitig laufen, nie parallel aufrufen.
2. Sammlung finden mit `list_collections(name_contains=…)`. **IDs nie fest verdrahten**, immer hier nachschlagen.
   - Monat „Oktober 2026“ / „2026/10“: Ordner-Sammlung wie „Aus Lightroom/10/2026“, sonst eine Sammlung mit dem Namen.
   - Reise: „Aus Lightroom/<Reise>“ oder die Sammlung mit dem Namen.
   - Nichts passt: den Eigentümer fragen oder `list_photos` mit engem Datum (ein Tag) versuchen.
3. Hilfssammlungen nachschlagen oder anlegen (`create_collection`, Trockenlauf, dann `confirm=true`):
   „<Sammlung> – Aussortieren (Vorschlag)“ und „Fotobuch Hoffis <Jahr>“ (gern unter der Gruppe des Jahres:
   `create_collection(name, parent="2026")`, vorhandene mit `move_collection`).
4. Fotos holen: `list_photos(collection_id=…, limit=100, offset=…)` in Seiten von 100. **Nie im ganzen Katalog suchen**
   (über 260.000 Fotos, Zeitüberschreitung nach 30 s). Nach Dateiname sortieren, dann liegen Serien zusammen.
5. **Notizdatei** im Scratchpad anlegen: pro Foto Dateiname, volle ID, Entscheidung. Alles, was später angewendet wird,
   kommt aus dieser Datei, nie aus dem Gedächtnis.

## 1. Durchsicht (nur lesen)
- `get_contact_sheet` mit **6 Fotos, cell=200, columns=6**, immer nur ein Aufruf zugleich (mehr oder parallel führt zu
  Zeitüberschreitungen, und das Plugin trennt die Verbindung).
- Pro Foto entscheiden und in die Notizdatei schreiben:
  - **5★** Spitzenfoto, Instagram/MHPhotos-tauglich. **4★** sehr gut. Bestehende Sterne **nie senken**.
  - **Serien/Doppelte**: pro Serie bleibt das beste Foto, der Rest geht in „Aussortieren (Vorschlag)“.
    Ebenso: unscharf, leerer Horizont, Reling im Bild, Fehlauslöser.
  - **Videos** überspringen.
  - **Familie** (Selfies, Gruppen, Porträts, die Kinder): höchstens 4★, kommt in „Fotobuch Hoffis <Jahr>“, standardmäßig
    nicht für Instagram/MHPhotos. **Familienregel: pro Anlass/Serie höchstens 4 Familienselfies o. ä. fürs Fotobuch
    behalten**, den Rest in „Aussortieren (Vorschlag)“. Ein wirklich schönes Familienfoto mit Hinweis vorschlagen
    („lieber eins zu viel“), der Eigentümer entscheidet.
  - **Fremde Personen** sind erlaubt, wenn es ein Spitzenfoto ist (Straßenfotografie).
  - **Instagram-Kandidat** (meist 5★): in der Notiz merken, mit Motivposition für den Zuschnitt (focus_x/focus_y).
- Nichts erfinden: nur beschreiben, was auf dem Blatt zu sehen ist. Im Zweifel nachfragen.

## 2. Sterne und Stichwörter (blockweise, nach ca. 50 bis 100 Fotos)
Pro Block eine Zusammenfassung zeigen, dann anwenden. Schreib-Tools laufen als Trockenlauf, `confirm=true` führt aus.
1. `set_rating` getrennt für 5★ und 4★ (je eine Liste).
2. `add_to_collection` für Fotobuch und für Aussortieren (höchstens 100 Fotos pro Aufruf).
3. Nach Freigabe der Sterne: `add_keywords` (5 bis 12 deutsche Stichwörter: Motiv, Ort, Stimmung, keine Hashtags;
   Orte gern hierarchisch, `create_keyword("Orte > Spanien > Barcelona")`) und `set_title_caption` nur für 5★ und 4★.
   Bestehende Titel nur mit `overwrite=true` ersetzen, wenn der Eigentümer das will.
4. Kurzbericht: Anzahl 5★/4★, Fotobuch, Aussortieren, übersprungene Videos.
Rückfragen nur, wo der Eigentümer entscheiden muss; sonst weiterarbeiten.

## 3. Instagram-Zuschnitte als virtuelle Kopien (Plugin ab 0.1.25)
1. Kandidaten aus der Notiz, Motiv mit `get_photo_previews` prüfen.
2. `crop_to_aspect(photo_ids, aspect="4:5", focus_x, focus_y)` (Feed) bzw. `aspect="9:16"` (Story), höchstens 10 Fotos
   und ein Fokuspunkt pro Aufruf. Trockenlauf, dann `confirm=true`. Original bleibt unverändert, die Kopie heißt
   „Insta 4:5“ bzw. „Story 9:16“.
3. Die Kopien entstehen als **Hintergrund-Job**. Antwort `state: running` oder `copies_pending` mit `job_id`:
   alle 30 bis 60 s `virtual_copy_job(job_id)` fragen, bis `state: done`. Danach den Zuschnitt auf die neuen Kopie-IDs mit
   `crop_to_aspect(copy_ids, aspect=…, focus_x, focus_y, virtual_copies=false)`. **Nie ein zweites Mal Kopien anlegen.**
4. Frische Kopien antworten einige Minuten mit `photo_busy`: kurz warten, dann erneut, nicht als Fehler werten.
5. `virtual_copy_timeout` oder `copy_pending` ohne Fortschritt: den Eigentümer die Kopie in Lightroom von Hand anlegen
   lassen (Cmd+'), ID über die Sammlung und den Dateinamen finden, weiter mit `virtual_copies=false`.
6. Kontrolle mit `get_photo_previews([original, kopie])`; das Ergebnis `used` zeigt das tatsächliche Format.
   Horizont begradigen: `crop_to_aspect(ids, aspect="original", angle=-2.5, virtual_copies=false)` (negativ = Horizont
   steigt nach rechts), erst an einem Foto mit dem Eigentümer abstimmen. Falsch gedrehte Fotos: `rotate_photos`.
7. Optional, nur auf Wunsch: S/W-Kopie (`create_virtual_copies` + `apply_develop_settings` mit `ConvertToGrayscale`),
   Presets auf Kopien (`list_presets`, `apply_preset`), Export (`export_photos(preset="instagram" | "story")`).

## 4. Aussortieren in Bögen
Gegenstand: die Sammlung „<Sammlung> – Aussortieren (Vorschlag)“ plus alle Fotos der Sammlung **ohne Sterne**.
1. Liste nach Dateiname bzw. Aufnahmereihenfolge sortieren, in Bögen von 6 bis 8 Fotos teilen und die Gesamtzahl nennen
   („233 Fotos, Bogen 1 von ca. 30“).
2. Pro Bogen: `get_contact_sheet` (cell=200, columns=6 bzw. 8), dann eine Tabelle:

   | Nr. | Dateiname | Vorschlag | Grund |
   |---|---|---|---|
   | 1 | IMG_1234.HEIC | Aus | Doppelt zu Nr. 2, leicht unscharf |
   | 2 | IMG_1235.HEIC | Behalten | Beste der Serie |

   Familienregel beachten (höchstens 4 Familienselfies pro Anlass fürs Fotobuch).
3. Der Eigentümer bestätigt oder korrigiert („ok“, „3 behalten“). Ergebnis in die Notizdatei. Erst dann den nächsten Bogen.
4. Alle paar Bögen anwenden, Trockenlauf zuerst: „Aus“ → `set_flag(ids, "rejected")` (rückgängig zu machen, löscht nichts),
   „Behalten“ ohne Sterne bekommt auf Wunsch `set_rating` 3 oder 4. Löschen der abgelehnten Fotos macht der Eigentümer
   selbst in Lightroom.
5. Nach jedem Bogen den Stand nennen („Bogen 5 von 30 fertig“), damit es in einem neuen Chat weitergehen kann.

## 5. Fotobuch, Kontaktabzüge und Abschluss
1. Fotobuch-Sammlung prüfen: `list_photos(collection_id=<Fotobuch>)`, Kontaktabzüge mit `get_contact_sheet` (bis 30 Fotos
   pro Blatt für die Übersicht, bei Verbindungsproblemen wieder 6). Familienregel und Doppelte kontrollieren.
2. Kontaktabzug der 5★ und der Instagram-Kopien als Übersicht zeigen.
3. Abschlussbericht: Anzahl 5★/4★, Stichwörter gesetzt, Instagram-Kopien (4:5/9:16), Fotobuch, abgelehnt, offen.
4. Instagram/MHPhotos nur vorschlagen (`instagram_candidates`, `mhphotos_schedule_instagram` ohne `confirm`);
   nichts geht ohne ausdrückliche Anweisung online. Wochenplan mit Posts, Stories und Reel: Abschnitt 6.

## 6. Instagram-Woche mit mhphotos.de („/mhphotos Instawoche Kreuzfahrt 2026“)
Beispiel: Posts Montag, Mittwoch, Freitag um 6 Uhr, abends die passende Story, Sonntagabend ein Wochenrückblick als Reel.
Zeiten in Europe/Berlin mit Offset angeben (Winter `+01:00`, Sommer `+02:00`). Konto `mhphotos`, außer der Eigentümer nennt `malou` oder `martin`.
1. **Serien wählen:** aus der Sammlung pro Post eine Serie von 3 bis 6 starken Fotos (meist 5★, keine Familie, abwechslungsreich:
   Orte, Licht, Motive). `mhphotos_instagram_queue` und `mhphotos_best_times` vorher ansehen, nichts doppelt planen.
2. **Zuschnitte:** pro Foto eine „Insta 4:5“-Kopie, für die Story eine „Story 9:16“-Kopie (Abschnitt 3).
3. **Texte pro Post** (Stil früherer Posts aus `mhphotos_instagram_posts` übernehmen, deutsch, kurz):
   Titel/erste Zeile, 2 bis 4 Sätze, Ort, 10 bis 20 passende Hashtags (Ort, Reise, Motiv, Fotografie), Markierungen als
   `@konto` im Text (Reederei, Schiff, Hafen, Orte), nur echte Konten, die der Eigentümer bestätigt; im Zweifel fragen.
4. **Plan zeigen**, eine Tabelle: Tag, Uhrzeit, Typ (Post/Story/Reel), Fotos (Dateinamen), Text, Hashtags, Markierungen,
   dazu die mhphotos.de-Angaben aus Schritt 5. Erst nach dem Ok des Eigentümers einplanen.
5. **Auf mhphotos.de** kommen alle Fotos, die auf Instagram gepostet werden, immer mit (Originale, nicht die Insta-Kopien):
   - Pro Foto festlegen: **Titel**, **Kategorie** (vorhandene Galerie, über `mhphotos_site_photos` bzw. `mhphotos_galleries`
     nachschlagen, nichts erfinden), **Beschreibung** (1 bis 3 Sätze, SEO: Ort, Motiv, Anlass natürlich formuliert),
     **Alt-Text** (was zu sehen ist, für Barrierefreiheit und Bildersuche), **Ort**, 5 bis 12 deutsche **Stichwörter**.
   - In Lightroom schreiben: `set_title_caption` (Titel, Beschreibung) und `add_keywords`, Trockenlauf, dann `confirm=true`.
   - `export_photos(photo_ids, confirm=true)` schickt die Originale in die MHPhotos-Inbox (Status „wartet“; Titel,
     Beschreibung, Ort und Stichwörter kommen über die JPEG-Metadaten mit). Mit `mhphotos_inbox(status="wartet")` die
     Inbox-IDs holen (dauert ggf. ein, zwei Minuten).
   - **Selbst veröffentlichen** (Connector mit `--allow-publish`): `mhphotos_galleries` für gültige Kategorien, dann
     `mhphotos_publish(item_ids, overrides={id: {title, description, alt, location, categories}})` als Trockenlauf;
     ist der Wochenplan freigegeben, mit `confirm=true` veröffentlichen. Die Seite kennt keine Stichwörter: SEO steckt in
     Titel, Beschreibung und Alt-Text.
   - Schon veröffentlichte Fotos korrigieren: `mhphotos_update_site_photo(photo_id, title=…, description=…, alt=…,
     categories=[…])`; einen Instagram-Post nachträglich verknüpfen: `mhphotos_link_instagram(instagram_post_id, photo_id)`.
   - Fehlen die Tools (`mhphotos_publish` nicht da), übernimmt der Eigentümer die Fotos in der MHPhotos-App; dann Titel,
     Kategorie, Beschreibung und Alt-Text in den Copy-und-Paste-Block (Schritt 9).
   - Das passiert **vor** den Posts, dann gibt es die Fotos am Sonntag für das Reel.
6. **Posts einplanen:** `mhphotos_schedule_instagram(caption, scheduled_at, photo_ids=[…Insta-Kopien in Reihenfolge…],
   mentions=[…], location_name=…)` (Karussell, 2 bis 10 Fotos; ein Einzelfoto mit `photo_id`), zuerst ohne `confirm`,
   nach dem Ok zum Plan mit `confirm=true`. Standard `needs_approval=true`: der Beitrag wartet in der MHPhotos-Warteschlange
   als „wartet auf Freigabe“, bis der Eigentümer ihn unter Admin › Social › Warteschlange freigibt; erst dann geht er zur
   geplanten Zeit raus. `mentions` markiert Konten im Bild, aber nur solche auf der MHPhotos-Liste vertrauter Konten;
   andere gehen nur als @Erwähnung in den Text. Meldet das Tool `approval_unsupported`, ist die MHPhotos-App noch nicht
   aktualisiert: dann nichts einplanen und dem Eigentümer Bescheid geben.
7. **Stories am Abend** (z. B. 19 Uhr, besser nach `mhphotos_best_times`): `mhphotos_schedule_instagram(post_type="story",
   photo_id=<Story-9:16-Kopie>, caption=…, scheduled_at=…)`. Einen anklickbaren Link oder Sticker zum Post vom Morgen kann der
   Connector nicht setzen: dem Eigentümer sagen, dass er den Post in der Instagram-App in die Story teilt oder den Link-Sticker
   dort ergänzt. Im Plan vermerken.
8. **Reel am Sonntagabend:** nach der Übernahme auf mhphotos.de die Fotos mit `mhphotos_site_photos(query)` finden,
   6 bis 12 der Woche in guter Reihenfolge (starkes erstes und letztes Bild), `mhphotos_create_reel(photo_ids, title, caption)`
   als Trockenlauf, dann `confirm=true`. `mhphotos_reel_jobs` zeigt „bereit“. Das Reel ist eine stumme Diashow; freigeben und
   auf Sonntagabend planen macht der Eigentümer im Content Studio. Musik nur, wenn er das MP4 dort lädt und in der App postet.
9. **Abschluss:** `mhphotos_instagram_queue` zeigen (alles mit Zeit). Dann ein **Copy-und-Paste-Block** pro Tag, jeweils als
   eigener Codeblock, damit er sich am Handy mit einem Tipp kopieren lässt:
   - mhphotos.de pro Foto: Titel und Link bzw. Kategorie (nur zum Nachsehen; ohne `mhphotos_publish` zum Übernehmen in der App).
   - Instagram-Post: Konten, die nicht markiert werden konnten (nicht auf der Liste vertrauter Konten), zum Nachtragen in der App.
   - Story: Text für die Story, Link-Sticker-Hinweis („Post von heute früh teilen“), Konten zum Erwähnen.
   - Reel: Titel und Text, Hinweis „im Content Studio freigeben, Sonntag 19 Uhr“.
   Texte, Hashtags und @Erwähnungen stehen bereits im geplanten Post; der Block ist nur für das, was der Connector nicht
   setzen kann. Nach dem Posten `add_keywords([ids], ["Instagram"])`.
Freigegebene Instagram-Beiträge erscheinen automatisch zur geplanten Zeit; rechtzeitig abbrechen mit `mhphotos_cancel_instagram`.
Im Abschlussbericht erinnern: „Bitte unter Admin › Social › Warteschlange freigeben (Filter „Zur Freigabe“).“

## Ganzes Jahr („/mhphotos 2026“)
Reise- und Monatssammlungen des Jahres über `list_collections` finden, **eine nach der anderen** mit den Phasen 1 bis 5
durchgehen, jeweils eigenes Aussortieren-Album; das Fotobuch ist eines für das ganze Jahr. Bei vielen Fotos die Reisen
auf getrennte Chats verteilen.

## Grenzen
Lightroom kann über das SDK keine Fotos verschieben oder löschen, und der Haken „Mit Lightroom synchronisieren“ geht nur
von Hand. Eigene Metadatenfelder (`set_custom_metadata`) sind derzeit abgeschaltet: Stichwörter oder Sammlungen nutzen.

## 7. Vorsortieren (auch nachts, Plugin ab 0.1.26)
Für den großen Rückstand ungesichteter Fotos, Monat für Monat. Es werden **keine Sterne gesetzt**, nur Prüf-Sammlungen gefüllt.
1. `presort_status()`: nimm `next[0]` (ein angefangener Monat kommt zuerst; `last_batch` sagt, wo es weitergeht).
2. `presort_month(month, batch_offset=last_batch)`: Serien und Blätter zu je 6 Fotos.
3. Pro Blatt `get_contact_sheet(photo_ids, columns=6, cell=200)`, Regeln wie in Abschnitt 1 (5★ = könnte online, Familie höchstens 4★, pro Serie nur das beste). Danach `presort_apply(month, top=[…], good=[…], reject=[…], series_rest=[…], batch=N, confirm=true)`.
4. Nach dem letzten Blatt `finished=true`. Pro Lauf höchstens etwa 40 Blätter, dann kurzer Bericht (Monat, Zahlen je Liste).
Der Eigentümer sieht morgens „Vorsortieren › <Monat> › Kandidat 5★ / Kandidat 4★ / Aussortieren (Vorschlag) / Serie – Rest (Vorschlag)“ und entscheidet in Lightroom.

## 8. Aufräumen und Verbinden (Plugin ab 0.1.26)
- Nach einer Katalog-Prüfung über alles: `catalog_audit_overview`, `catalog_folder_moves` (ganze Ordner zuerst), `archive_candidates`.
- Stichwörter: `keyword_check_start` → `keyword_check_results("duplicates")` → `merge_keywords` nach Freigabe, danach in Lightroom „Nicht verwendete Stichwörter löschen“.
- Was online ist: `mark_online_in_lightroom` (Trockenlauf, dann `confirm=true`), z. B. einmal pro Woche.
- MHPhotos: `spot_photos` (Spots ohne gutes eigenes Foto für den Fotoplaner), `gear_usage`, `trip_outline` → `mhphotos_create_blog_draft` (bleibt Entwurf).
- Ab 0.1.27: `duplicate_folders` (doppelt importierte Ordner), `missing_files_by_drive` (nach `catalog_audit_start(check_files=true)`), `gps_gaps` → `fill_gps_gaps` nach Freigabe, `print_candidates` → `print_candidates_collections`, `weekly_report`.
- Ab 0.1.29: `photo_stats` (Foto-Statistik als Offline-HTML-Seite aus dem Katalog-Schnappschuss: Kameras, Objektive, Brennweiten, Blende/ISO, Trefferquote, goldene Stunde, ungenutzte Objektive, Festbrennweiten-Tipp). Blende/ISO/Zeit brauchen Plugin 0.1.29 und eine neue `catalog_audit_start()`.

## 9. Fotokalender („Kalender 2027“, Plugin ab 0.1.27)
1. `calendar_candidates(year=2027, per_month=8)`: pro Monat die besten Fotos aus diesem Monat (alle Jahre), quer, groß genug für A3.
2. Pro Monat `get_contact_sheet(photo_ids, columns=6, cell=200)` ansehen, 2–3 Favoriten mit Begründung vorschlagen (Jahreszeit passt, keine zwei ähnlichen Motive hintereinander, Abwechslung bei Orten und Farben).
3. `calendar_collections(year=2027, confirm=true)` mit allen Vorschlägen, oder nach Martins Wahl mit `photo_ids_by_month`. Das endgültige Bild je Monat wählt Martin in Lightroom.

## Eisenregeln
- Alles Schreibende zuerst als Trockenlauf, `confirm=true` erst nach der Freigabe oder wenn der Block dem Vereinbarten entspricht.
- Keine Löschung, kein Verschieben. Veröffentlichen und Einplanen nur nach ausdrücklichem Ok zum gezeigten Plan (Abschnitt 6). Aussortieren heißt nur „Vorschlag“ bzw. Flagge „abgelehnt“.
- Sterne nie senken, Originale nie überschreiben (Bearbeitungen auf virtuellen Kopien), IDs immer frisch aus Lightroom.
- Bei Zeitüberschreitung nicht blind wiederholen: erst `connection_status`, dann prüfen, ob die Änderung angekommen ist.
- Jede Änderung ist mit `undo_last_change` rückgängig zu machen.
