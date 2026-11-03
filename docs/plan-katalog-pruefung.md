# Plan: Katalog-Prüfung (falscher Ordner, Duplikate)

Stand: 2026-11-03 · Status: **gebaut** (Plugin 0.1.2, Werkzeuge `catalog_audit_*`; Ablauf: `docs/workflows.md` Nr. 11)

## Vorgaben vom Eigentümer

| Frage | Antwort |
|---|---|
| Ordnerstruktur | `JJJJ/JJJJ-MM/JJJJ-MM-TT/` (z. B. `2026/2026-10/2026-10-14/`), auf dem NAS genauso wie im Katalog |
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
2. Dann alle Jahre; Duplikat-Hash nur auf Kandidaten.
