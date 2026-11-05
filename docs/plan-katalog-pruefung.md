# Plan: Katalog-Prüfung (falscher Ordner, Duplikate)

Stand: 2026-11-03 · Status: **gebaut** (Plugin 0.1.2, Werkzeuge `catalog_audit_*`; Ablauf: `docs/workflows.md` Nr. 11)

## Vorgaben vom Eigentümer

| Frage | Antwort |
|---|---|
| Ordnerstruktur | `Lightroom/JJJJ-JJJJ/JJJJ/JJJJ-MM/JJJJ-MM-TT/` mit Jahrzehnt-Ordner (z. B. `2020-2029/2026/2026-10/2026-10-01/`; vor 2000: `1900-1999`), auf dem NAS genauso wie im Katalog |
| Maßgebliches Datum | **Aufnahmedatum** (`dateTimeOriginal`), nicht Importdatum |
| Katalog-Größe | ca. **268.270 Fotos** (Jahre ≤ 2020 bis 2026; 2023/2024 je ~30.000) |

## Grenze des Lightroom-SDK

Das SDK kann Fotos **weder verschieben noch aus dem Katalog entfernen**. Claude prüft und
sortiert in Prüf-Sammlungen; verschoben (Drag & Drop im Ordner-Panel) und entfernt wird in
Lightroom von Hand. Nie im Finder/NAS direkt verschieben – sonst verliert der Katalog die
Verknüpfung.

## Bausteine

1. **Plugin-Befehl `audit_catalog`** (nur lesen), seitenweise (z. B. 2.000 Fotos pro Seite,
   Cursor), läuft als asynchroner Lightroom-Task. Pro Foto: `uuid`, Ordnerpfad, Dateiname,
   `dateTimeOriginal`, Kamera (Modell/Seriennummer), Pixelmaße, Dateigröße, Dateityp,
   virtuelle Kopie ja/nein, Datei vorhanden ja/nein, Bearbeitet/Sterne/Stichwörter/Sammlungen.
2. **Ergebnis bleibt auf dem Mac** (`~/.mhphotos-lightroom-mcp/audit/…json`); über den
   Connector gehen nur Zusammenfassungen und gezielte Listen.
3. **Ordner-Regel:** Soll-Pfad aus Aufnahmedatum (lokale Zeit) = `JJJJ/JJJJ-MM/JJJJ-MM-TT`.
   Abweichung nach Ebene melden: falsches Jahr / falscher Monat / falscher Tag.
   Gesondert (nicht automatisch zuordnen): kein Aufnahmedatum, unplausibles Datum
   (vor 1990, in der Zukunft, 1970-01-01), fehlende Dateien.
4. **Duplikate, zweistufig:**
   - Kandidaten: gleiche Aufnahmezeit (Sekunde) + Kamera + Pixelmaße (+ Dateigröße).
   - Bestätigen: SHA-256 über Größe + erstes und letztes MiB, nur für Kandidaten (NAS-Last klein halten).
   - „Möglich“ (andere Bytes): nur bei gleichem Dateinamen mit Kopie-Endung (`IMG_1-2`, `IMG_1 (1)`),
     sonst wären Serienbilder derselben Sekunde Fehlalarme.
   - Nicht als doppelt zählen: virtuelle Kopien, RAW+JPEG-Paare, Serienbilder mit anderem Inhalt.
   - Behalten-Vorschlag: Foto mit Entwicklung, Sternen, Stichwörtern, Sammlungen, Veröffentlichung.
5. **Prüf-Sammlungen** (Schreib-Werkzeuge): `Prüfen › Ordner › gehört nach JJJJ-MM-TT`,
   `Prüfen › Duplikate › entfernen`, `Prüfen › ohne Datum`, `Prüfen › Datei fehlt`.

## Reihenfolge

1. Erst ein Jahr testen (z. B. 2020, 4.880 Fotos) → Laufzeit und Trefferqualität prüfen.
   Mit `year` liest das Plugin (ab 0.1.6) erst nur Aufnahmezeit und Pfad des ganzen Katalogs (schnelle Blöcke)
   und die Details nur für das Jahr; Duplikate werden dann nur innerhalb des Jahres gesucht.
   Seit 0.1.7 läuft die Jahressuche in Schritten (`audit_scan`): `catalog_audit_status` zeigt Fortschritt in Prozent,
   `catalog_audit_cancel` stoppt den Lauf beim nächsten Schritt.
2. Dann alle Jahre; Duplikat-Hash nur auf Kandidaten.

## Zweite Ausbaustufe (gebaut 2026-11-03, Plugin 0.1.3; Punkt 4 noch offen)

1. **Keine echten Fotos aussortieren** (Screenshots, Messenger-Bilder, Downloads):
   Anzeichen kombinieren, nicht einzeln werten:
   - kein Kameramodell
   - keine Belichtungsdaten (ISO, Blende)
   - PNG/HEIC-Screenshot
   - Dateiname wie `Screenshot…`, `IMG-…-WA…`
   - typische Bildschirmmaße (z. B. 1170×2532, 1920×1080)
   - kleine Auflösung

   Ergebnis: Sammlung „Prüfen › Sonstiges › keine Kamera-Fotos“ (`kind="non_photo"`, mit Gründen).
   Regel: Screenshot-Dateiname genügt; sonst „keine Kamera“ plus mindestens zwei weitere Anzeichen.
2. **RAW+JPEG gleichzeitig aufgenommen, nur RAW behalten**:
   - Ein JPEG gilt als Begleiter, wenn ein RAW mit gleicher Aufnahmesekunde, gleicher Kamera und gleichem Dateistamm existiert, meist im selben Ordner.
   - Ergebnis (`kind="jpeg_with_raw"`): „Prüfen › Duplikate › JPEG neben RAW – entfernen“ und „… – erst prüfen“.
   - Ausnahme, nur melden: Das JPEG hat Sterne, Stichwörter, Entwicklung oder Sammlungen und das RAW nicht.
3. **Re-importierte Exporte** (`kind="reimported_export"`): JPEG mit gleichem Dateistamm und gleicher Aufnahmesekunde
   wie ein RAW, aber in einem anderen Ordner oder deutlich kleiner. Sammlung „Prüfen › Sonstiges › Export wieder importiert“.
4. **Excire Search** (läuft im Katalog des Eigentümers). Was Excire genau in den Katalog schreibt, noch prüfen:
   - Schreibt Excire Stichwörter in Lightroom, können die Werkzeuge danach filtern, z. B. „Text/Dokument“ zur Screenshot-Erkennung.
   - Excire sucht visuell ähnliche Bilder (anderer Ausschnitt, andere Größe). Das ergänzt unsere exakte Duplikatsuche.
   - Excire-Suche mit Beispielbild könnte beim Instagram-Abgleich die Originale finden.

## Weitere Abläufe (gebaut 2026-11-03, Plugin 0.1.4)

Speicherplatz-Bilanz in der Katalog-Prüfung, Sichten nach dem Import, Stichwörter/Titel/Bildunterschriften,
Best-of, Instagram-Wochenplan, Orte nachtragen: `docs/workflows.md` Nr. 11–16. Werkzeuge: `photos_by_date`,
`suggest_locations`, `set_location`, `instagram_candidates`, `mhphotos_best_times`; Instagram-Planung über den
Connector mit `--allow-instagram`.

## Offene Punkte (Stand 2026-11-03)

- Mac:
  - `git pull --ff-only`
  - `./scripts/rebuild-mac.sh --allow-writes --mhphotos-url https://mhphotos.de --allow-instagram --apply --start`
  - In Lightroom: Zusatzmodul-Manager → Plugin neu laden
  - Connector in claude.ai neu verbinden: Schreib-Werkzeuge auf „fragen“, Lese-Werkzeuge auf „immer erlauben“;
    `mhphotos_schedule_instagram` und `mhphotos_cancel_instagram` unbedingt auf „fragen“
- n8n, Karte „In Inbox hochladen“: Body-Feld `instagramId` (Ausdruck in `docs/examples/n8n-jpegs-to-inbox.json`), dann Publish.
- Erster Probelauf: „Starte die Katalog-Prüfung für 2020“. macOS-Frage nach Netzwerk-Volumes erlauben.
  Danach Stichproben ansehen: `non_photo` (Fehlalarme?), `jpeg_with_raw`, `wrong_folder` mit `day_diff`.
- Echte Tests noch offen:
  - Ob Lightroom GPS über das SDK schreiben lässt (`set_location` meldet `gps_written`)
  - Erste Datumsabfrage (`photos_by_date`) baut einen Index über 268.000 Fotos: Dauer messen
  - Instagram-Wochenplan: Kandidaten, Duplikat-Prüfung, ein Probe-Post als Probelauf
  - XMP-Presets nach Lightroom-Neustart
  - Schreib-Werkzeuge über den Connector
  - Instagram-Abgleich
- n8n (apps-platform#2586): drei Workflows importieren (Status-Push, Montagsbericht, Fehler-Push), Credential
  „MHPhotos – Admin-Token“ wählen, Publish, bei allen Workflows *Error Workflow* = „MHPhotos – Fehler-Push“.
  Reel-Renderer als Dienst `reel-renderer` im n8n-Stack starten.
- Auto-Export über n8n: `./scripts/rebuild-mac.sh --hot-folder --apply --start`, im Plugin Auto-Export für die
  Sammlung „→ MHPhotos“ einrichten; alten Inbox-Watcher stoppen, falls er läuft.
- Gemergte Branches auf GitHub löschen.
- Später:
  - Karussell: gebaut (`photo_ids` in `mhphotos_schedule_instagram`).
  - Reels: Foto-Diashow ohne Musik, Renderer in apps-platform#2584 (`infra/mhphotos-reel-renderer`). Auf Hetzner
    einrichten: Admin-Token als Secret-Datei, `docker compose up -d --build`, Probelauf mit `--once`.
    Musik: Reel im Content Studio herunterladen und in der Instagram-App posten.
  - Löschen in der iOS-Admin-Ansicht: gebaut in apps-platform#2582, Swift-Test noch auf dem Mac laufen lassen (`swift test`)
- Erledigt (Plugin 0.1.5): Aufnahmezeit überall als Kamera-Uhrzeit statt UTC. Am echten Katalog einmal prüfen: ein Foto kurz nach Mitternacht muss bei `get_photo_metadata` den richtigen Tag zeigen.

## Struktur v2 und weitere Prüfungen (Stand 2026-11-04)

Nach dem Jahreslauf für 2020 (4.901 Fotos) angepasst. Der Katalog hat Jahrzehnt-Ordner (`2020-2029`), darunter aber
Jahre aus anderen Jahrzehnten, dazu Jahres-Ordner auf oberster Ebene, `ohne Datum` und `vor 2000`.
- **Soll-Pfad:** `<Jahrzehnt>/<Jahr>/<Jahr-Monat>/<Tag>`. `wrong_folder` hat zusätzlich die Stufen `decade`
  (Jahr stimmt, Jahrzehnt-Ordner falsch oder fehlt) und `no_day` (Monat stimmt, aber kein Tagesordner).
  Jeder Eintrag nennt `expected_path`. `catalog_audit_collections(kind="wrong_folder", level=…)` legt Sammlungen
  je Stufe an.
- **Videos** ohne Aufnahmedatum (`video_no_date`): Lightroom kennt dort kein Aufnahmedatum; eigene Gruppe statt `no_date`.
- **Datum im Dateinamen** (`date_in_name`, z. B. `2019-12-08_19-35-23_000.jpeg`): `name_date` und `folder_fits`.
  Das Datum selbst setzt man in Lightroom (*Metadaten > Aufnahmezeit bearbeiten*), danach prüft ein neuer Lauf den Ordner.
- **Nahezu gleich** (`near_duplicates`): gleiche Aufnahme und Namensstamm (`IMG_5.jpg`, `IMG_5-2.jpg`), Dateigröße
  auf 1 % gleich, nur Metadaten anders. Sammlungen „nahezu gleich – entfernen / behalten“.
- **Aufnahmedatum aus dem Dateinamen** (Test am 2026-11-04): Lightroom lässt `dateTimeOriginal` über das SDK **nicht**
  schreiben (`unknown metadata key`). Stattdessen schreibt `write_name_dates_to_files` (`--allow-file-edits`, braucht
  `exiftool` auf dem Mac) das Datum in die JPEG-Datei, nach einer Sicherung in `~/.mhphotos-lightroom-mcp/file-backups`.
  Nur JPEGs ohne Aufnahmezeit, nur aus `date_in_name` (mit Uhrzeit), erst 1 Foto, höchstens 50 pro Aufruf. Danach in
  Lightroom die Sammlung „Prüfen › Sonstiges › Datum in Datei geschrieben“ markieren und *Metadaten > Metadaten aus
  Datei lesen*, dann die Prüfung für das Jahr wiederholen. **Nicht möglich:** Dateien verschieben oder umbenennen
  (Lightroom-SDK); „Ordner synchronisieren“ würde neue Fotos anlegen und Bearbeitungen verlieren.

## Offene Punkte (Stand 2026-11-04, abends)

**Lightroom-Verbindung (wichtig für alles Weitere)**
- Bei längeren Aufrufen kam mehrfach „Lightroom did not answer … within 30s“ und danach „plugin is not connected“.
  Vermutung: App Nap drosselt Lightroom im Hintergrund. Vorbereiten: Lightroom vorn lassen, `caffeinate -di` im Terminal,
  App Nap für Lightroom ausschalten (`defaults write <Bundle-ID> NSAppSleepDisabled -bool YES`; Bundle-ID mit
  `osascript -e 'id of app "Adobe Lightroom Classic"'` ermitteln), Lightroom neu starten.
- `write_name_dates_to_files` in Blöcken zu **15** statt 50: 50 dauern über das NAS länger als die 60 s des Connectors.
- In den claude.ai-Einstellungen zeigte der Connector „keine Tools“, obwohl die Sitzung ihn nutzen konnte: trennen und neu verbinden.

**Datum aus dem Dateinamen (2020, 545 Kandidaten von 598)**
- Geschrieben: 10 Fotos sicher (1 eingelesen, 9 noch nicht). Ein Aufruf mit 50 lief in die Zeitgrenze, deshalb ist unklar, wie
  viele davon schon in den Dateien stehen. Das ist unkritisch (Sicherung in `~/.mhphotos-lightroom-mcp/file-backups`, schon
  beschriebene Dateien werden übersprungen), aber diese Fotos fehlen in der Sammlung „Datum in Datei geschrieben“.
  Abhilfe: `catalog_audit_collections(kind="date_in_name", confirm=true)` legt eine Sammlung mit allen Fotos mit Datum im Namen an,
  diese komplett markieren (Cmd+A) und *Metadaten > Metadaten aus Datei lesen*.
- 53 Namen mit Platzhalterdatum (z. B. `1899-01-01_…`) werden bewusst ausgelassen; ansehen und von Hand klären.
- Danach die Prüfung für 2020 wiederholen: Die Fotos gehören nach 2019 (`2010-2019/2019/…`).

**Ordnerstruktur**
- Lightroom kann Ordner nicht zusammenführen („Ein Ordner mit dem Namen … existiert bereits“). Dann Unterordner (Monatsordner)
  oder Fotos (Unterordner einbeziehen, Cmd+A) auf den vorhandenen Zielordner ziehen, leere Quellordner entfernen.
  Verschieben am NAS plus „Synchronisieren“ ist keine Lösung (neue Fotos, Bearbeitungen weg); nur „Fehlenden Ordner suchen“ je Ordner.
- Jahresordner auf oberster Ebene (`2010`, `2011`, `2022`, `2023`, `2026` …) in ihre Jahrzehnt-Ordner bringen.
- 2020: 3.373 Fotos ohne Tagesordner, 8 mit falschem Jahr (Mannschaftsfoto, Videos). Sammlungen pro Tag oder Monat nur nach Okay anlegen.
- Prüfung für die übrigen Jahre (2021–2026, 2010–2019, vor 2000) und eine Liste der Verschiebungen auf Ordner-Ebene.

**Aufräumen aus der Prüfung 2020** (alles nur nach Okay, nichts wird gelöscht oder verschoben)
- 194 nahezu gleiche Kopien (202 zum Entfernen), 8 bestätigte und 27 mögliche Duplikate, 411 Nicht-Fotos, 124 Videos ohne Datum,
  149 sonstige ohne Datum, 5 Fotos im Ordner „ohne Datum“ mit Aufnahmedatum.

**Neue Aufgabe: Sammlung „Aus Lightroom/Kreuzfahrt 2026“ analysieren**
- 843 Fotos (DNG, HEIC, MOV), iPhone 17 Pro und iPhone 13, Aufnahmen ab 19.10.2026, GPS meist vorhanden, Stichwörter teils schon da.
  Mögliche Auswertung: Best-of, Aussortieren, Stichwörter/Titel, Orte prüfen, Instagram-Wochenplan.

**Weiter offen (aus früheren Runden)**
- n8n: Workflows importieren (Status-Push, Montagsbericht, Fehler-Push), Reel-Renderer auf Hetzner starten, Swift-Test für das Löschen in der iOS-Ansicht (#2582).
- Excire-Plugin bewerten, gemergte Branches auf GitHub löschen.

## Auswahlregeln des Eigentümers (gelten für alle Auswertungen)

- **MHPhotos (Homepage) und Instagram: standardmäßig keine Familie.** Fotos mit der Familie (Selfies, Gruppen, Porträts, die Kinder) werden nicht
  automatisch dafür vorgesehen. **Fremde Personen sind erlaubt, wenn es ein Spitzenfoto ist** (Straßenfotografie: Person in der Gasse, verregnete Gasse
  mit Regenschirmen, Menschen als Teil der Szene). Zweifelsfälle (gehört die Person zur Familie?) frage ich nach.
- **Lieber ein Foto zu viel vorschlagen:** Ein wirklich schönes Foto mit Familie (zum Beispiel ein Kind von hinten vor einem Wandbild) schlage ich mit
  Hinweis vor. Eigentümer entscheidet; es geht nichts automatisch online.
- **Gute Fotos mit Personen** (Familie, Selfies, Gruppenbilder) in die eigene Sammlung **„Fotobuch Hoffis 2026“** legen, als Vorrat für ein Fotobuch.
- **Sterne:** 5 = Spitzenfoto (Instagram/MHPhotos-tauglich), 4 = sehr gut (Reserve). Bestehende Sterne nie senken. Gelungene Familienfotos
  bekommen höchstens 4 Sterne und stehen zusätzlich im Fotobuch; 5 Sterne sind für Fotos reserviert, die online gehen könnten. Filter für
  Instagram-Kandidaten und MHPhotos-Export nutzen 5 Sterne, treffen also keine Familienfotos, außer der Eigentümer gibt eines frei.
- **Doppelte Serien** (Selfie-Reihen, Mehrfachaufnahmen): pro Serie bleibt das beste Foto, die übrigen kommen in
  „Kreuzfahrt 2026 – Aussortieren (Vorschlag)“. Gelöscht wird nichts; das entscheidet der Eigentümer in Lightroom.
- Lightroom hat in der Sammlung „Kreuzfahrt 2026“ schon automatische Stichwörter: `Person` (210 von 843) und `Gesicht` (164). Sie helfen beim
  Vorsortieren, ersetzen die Sichtprüfung aber nicht.
- Sammlungen für die Handy-Synchronisierung brauchen den Haken „Mit Lightroom synchronisieren“ von Hand (das SDK kann ihn nicht setzen).
