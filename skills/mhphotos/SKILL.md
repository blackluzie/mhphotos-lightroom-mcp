---
name: mhphotos
description: Foto-Durchsicht mit dem MHPhotos-Lightroom-Connector. Aufruf zum Beispiel "/mhphotos 2026/10" oder "/mhphotos Kreuzfahrt 2026" oder "alle Fotos aus 2026/09 Südtirol in ein Album legen und optimieren". Geht einen Monat, eine Sammlung oder einen Ordner durch, bewertet mit Sternen, schlägt Aussortieren, Fotobuch, Instagram-Kandidaten, Zuschnitte, Begradigung, S/W und Presets vor, wendet es blockweise an und vergibt danach Titel und Stichwörter.
---

# /mhphotos: Fotos durchgehen und veredeln

Argument: ein Monat (`2026/10`), ein Jahr (`2026`), eine Sammlung („Kreuzfahrt 2026“) oder ein Ordner. Ohne Argument frage zuerst, was gemeint ist.
Antworte auf Deutsch. Nichts wird gelöscht oder veröffentlicht; das entscheidet der Eigentümer.

## 0. Vorbereitung
1. `connection_status`. Nicht verbunden: den Eigentümer bitten, in Lightroom das Plugin neu zu laden (Zusatzmodul-Manager → „Neu laden“), danach den Connector in claude.ai neu zu verbinden. Es kann immer nur **ein** Befehl gleichzeitig laufen.
2. `list_collections` (mit `name_contains`). **IDs nie fest verdrahten**, immer hier nachschlagen.
   Die Sammlungen „Fotobuch Hoffis <Jahr>“ und „<Sammlung> – Aussortieren (Vorschlag)“ gibt es oder sie werden mit `create_collection` angelegt (oberste Ebene).
   Der Haken „Mit Lightroom synchronisieren“ lässt sich nur von Hand setzen; dem Eigentümer sagen.
   **Intelligente Sammlungen** (`create_smart_collection`, z. B. „Top 2026“: `min_rating=4`, Datum 2026) füllen sich selbst; Stichwort-Hierarchie mit `create_keyword("Orte > Spanien > Barcelona", synonyms=[…])` und `add_keywords` mit demselben Pfad; `build_smart_previews` für Vorschauen ohne NAS; `import_photo` nimmt eine Datei in den Katalog auf.
   **Sammlungsgruppen (Plugin ab 0.1.15):** `create_collection(name, parent="2026")` legt eine Sammlung unter der Gruppe „2026“ an (fehlende Gruppen entstehen, Pfade wie „2026/Reisen“ gehen), `move_collection(collection_id, parent="2026")` verschiebt eine vorhandene Sammlung hinein (etwa „Fotobuch Hoffis 2026“). Beides als Trockenlauf, dann `confirm=true`. Ältere Plugin-Version: der Aufruf mit `parent` schlägt fehl, dann den Eigentümer das Plugin aktualisieren lassen.
3. **Fotos holen über die Ordner-Sammlung** (ab Plugin 0.1.16 versucht `list_photos` ohne Sammlung zuerst Lightrooms Index; geht das nicht, ist es der langsame Vollscan, dann weiter über die Sammlung). Früher: nie eine Suche im ganzen Katalog. Der Katalog hat über 260.000 Fotos; `photos_by_date` und `list_photos` mit Datumsfilter über alles laufen in die Zeitüberschreitung (30 s).
   Monatssammlungen heißen z. B. „Aus Lightroom/09/2026“ oder „Aus Lightroom/<Reise>“. `list_photos(collection_id=…, captured_from=…, captured_to=…, limit=100)` in Seiten von 100 abrufen. Die Reihenfolge ist zufällig, Serien liegen verstreut.
   Ist keine passende Sammlung da: den Eigentümer fragen oder `list_photos` mit engem Datum (ein Tag) versuchen.

## 1. Durchsicht (nur lesen)
- `get_contact_sheet` mit **6 Fotos, cell=200, columns=6**, immer nur ein Aufruf zugleich (bei mehr Fotos oder parallel kommt es zu Zeitüberschreitungen und das Plugin trennt die Verbindung).
- Pro Foto entscheiden und **in einer Notizdatei festhalten** (Scratchpad), mit Dateiname und voller ID, damit nichts verloren geht:
  - **5★**: Spitzenfoto, Instagram/MHPhotos-tauglich. **4★**: sehr gut. Bestehende Sterne **nie senken**.
  - **Serien/Doppelte** (Selfie-Reihen, Mehrfachaufnahmen): pro Serie bleibt das beste Foto, der Rest kommt in „Aussortieren (Vorschlag)“. Auch leerer Horizont, unscharf, Reling im Bild.
  - **Videos** überspringen, nicht bewerten.
  - **Familie** (Selfies, Gruppen, Porträts, die Kinder): höchstens 4★ und in „Fotobuch Hoffis <Jahr>“. Standardmäßig nicht für Instagram/MHPhotos.
    Ist ein Familienfoto wirklich schön (zum Beispiel ein Kind von hinten vor einem Wandbild): **mit Hinweis vorschlagen** („lieber ein Foto zu viel“), der Eigentümer entscheidet.
  - **Fremde Personen** sind erlaubt, wenn es ein Spitzenfoto ist (Straßenfotografie: Person in der Gasse, Regenschirme).
- Beim Beschreiben der Fotos nichts erfinden: nur sagen, was auf dem Blatt zu sehen ist. Im Zweifel nachfragen.

## 2. Anwenden (blockweise, nach ca. 50 bis 100 Fotos)
Pro Block: Zusammenfassung zeigen, dann anwenden (die Schreib-Tools laufen als Trockenlauf, `confirm=true` führt aus):
1. `set_rating` getrennt für 5★ und 4★ (je eine Liste).
2. `add_to_collection` für Fotobuch und für Aussortieren.
3. Kurzbericht: Anzahl 5★/4★, Fotobuch, Aussortieren, übersprungene Videos.
Eigentümer-Rückfragen nur, wo er entscheiden muss. Sonst weiterarbeiten.

## 3. Veredeln (nach der Freigabe der Sterne)
- **Titel und Stichwörter** (`set_title_caption`, `add_keywords`) nur für 5★ und 4★, erst nach Freigabe.
- **Schwarzweiß-„Klassiker“**: `create_virtual_copies` (Name z. B. „S/W“), danach `apply_develop_settings` mit `ConvertToGrayscale`; Kandidaten sind Straßenszenen, Silhouetten, starke Kontraste.
- **Presets** für 5★: `list_presets`, `apply_preset` (auf Kopien, nicht auf Originalen), danach `get_photo_previews` zur Kontrolle.
- **Instagram-Formate** (siehe `docs/workflows.md`, Abschnitt 19):
  - `crop_to_aspect(photo_ids, aspect="4:5" | "9:16", focus_x, focus_y)`. Die Vorschau/`get_photo_previews` zeigt den Ausschnitt; `focus_x/y` 0 = links/oben, 1 = rechts/unten.
  - Ein Aufruf mit Kopie dauert etwa eine Minute und läuft oft in die Zeitüberschreitung, die Kopie ist dann trotzdem angelegt. Sie wiederzufinden ist schwer (Suche im ganzen Katalog ist zu langsam): die Kopie in die Sammlung ziehen lassen und dort über den Dateinamen suchen. **Vorsicht:** Danach den Zuschnitt auf die Kopie mit `virtual_copies=false` anwenden, nie ein zweites Mal Kopien anlegen.
  - Das Ergebnis enthält `used` (Größe, Zuschnittwerte, Größe nach dem Zuschnitt). Weicht das Format ab, gibt das Tool einen Fehler aus.
  - **Horizont begradigen**: `crop_to_aspect(photo_ids, aspect="original", angle=-2.5, virtual_copies=false)`. Negativer Winkel = Horizont, der nach rechts ansteigt, wird waagerecht. Neigung aus der Vorschau schätzen (Anstieg in Pixeln durch Breite) und in 0,5°-Schritten nachstellen. Immer mit dem Eigentümer an einem Foto abstimmen.
  - **Falsch gedrehte Fotos** (Ausrichtung): `rotate_photos` (90/180/270° im Uhrzeigersinn), nur nach Prüfung auf dem Blatt.
- **Instagram/MHPhotos**: nur vorschlagen (`instagram_candidates`, `mhphotos_inbox`); nichts geht ohne ausdrückliche Anweisung online.

## 4. Album für einen Monat oder eine Reise („alles aus 2026/09 Südtirol in ein Album“)
1. Passende Ordner-Sammlung suchen (`list_collections`, z. B. „Aus Lightroom/09/2026“), die Fotos dort holen.
2. `create_collection("Südtirol 2026")` (oberste Ebene), `add_to_collection` in Blöcken von höchstens 100 Fotos.
3. Danach wie oben: Durchsicht, Sterne, Aussortieren, Veredeln.
4. „Optimieren“ heißt hier: Vorschläge und Presets auf **virtuellen Kopien** oder mit `apply_develop_settings` (Belichtung, Kontrast, Dunst usw.), mit Vorher/Nachher-Kontrolle. Originale nicht überschreiben, wenn der Eigentümer nichts anderes sagt. Jede Änderung ist mit `undo_last_change` rückgängig zu machen.
5. Grenzen: Lightroom kann über das SDK keine Fotos verschieben oder löschen und keine automatische Tonwertkorrektur („Auto“) auslösen; das bleibt Handarbeit des Eigentümers.

## 5. Das ganze Jahr („/mhphotos 2026“)
1. `list_collections` lesen: Reise- und Monatssammlungen des Jahres finden (z. B. „Aus Lightroom/Kreuzfahrt 2026“, „…/Spanien 2026“, „…/09/2026“).
2. Gruppe „2026“ anlegen und die Sammlungen „Fotobuch Hoffis 2026“ und „<Reise> – Aussortieren (Vorschlag)“ sowie die Reise-Alben per `move_collection` hineinlegen; für jede Reise ein eigenes Album `create_collection(name, parent="2026")`.
3. **Eine Reise nach der anderen** durchgehen (Abschnitte 1 bis 3), jeweils mit eigenem Aussortieren-Album; das Fotobuch ist eines für das ganze Jahr. Pro Reise ein kurzer Bericht; bei sehr vielen Fotos die Reisen auf getrennte Chats verteilen.
4. Titel, Stichwörter, S/W und Formate erst nach Freigabe der Sterne.

## Eisenregeln
- Alles Schreibende zuerst als Trockenlauf, `confirm=true` erst nach der Freigabe oder wenn der Block dem Vereinbarten entspricht.
- Keine Löschung, kein Verschieben, keine Veröffentlichung. Aussortieren heißt nur „Vorschlag“.
- Ratings nie senken, Fotos nie überschreiben, IDs immer frisch aus Lightroom holen, nie aus dem Gedächtnis.
- Bei Zeitüberschreitung nicht blind wiederholen: erst `connection_status`, dann prüfen, ob die Änderung angekommen ist.
