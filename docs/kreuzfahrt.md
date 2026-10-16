# Unterwegs ohne Lightroom Classic (Kreuzfahrt)

Ziel: Fotos aus Lightroom Mobile, Claude sucht die besten aus, bearbeitet sie, übernimmt sie auf mhphotos.de oder
plant sie für Instagram. Hier steht ehrlich, was davon geht, was nicht, und was vor der Abreise zu tun ist.

## Was nicht geht
- **Kein direkter Zugriff auf Lightroom Mobile.** Lightroom Mobile speichert in Adobes Cloud. Adobes Schnittstelle dafür
  ist auf Partner-Apps ausgelegt; einen Zugang für eine persönliche Anwendung konnte ich nicht belegen (siehe
  [cloud-assessment.md](cloud-assessment.md)). Wir bauen deshalb nicht darauf.
- **Keine Lightroom-Entwicklung ohne Lightroom.** Das Plugin steuert Lightroom Classic und das läuft nur auf deinem Mac.
  Ohne Classic gibt es nur **einfache Bearbeitungen** (Belichtung, Kontrast, Sättigung, Wärme, Schärfen, Zuschnitt) an
  der Instagram-Kopie. Das ist kein Ersatz für eine Lightroom-Entwicklung, für Urlaubsfotos aber brauchbar.

## Zwei Wege, wie die Fotos zu mir kommen
**Weg A: Dein Mac bleibt zuhause an.** Lightroom Mobile lädt in die Adobe-Cloud, Lightroom Classic holt die Originale
aus der Cloud, dein vorhandener Mac-Dienst schiebt sie in die MHPhotos-Inbox. Ab dort arbeite ich mit den Inbox-Tools.
Voraussetzungen, die ich nicht prüfen kann:
- Der Mac läuft zwei Wochen durch, Lightroom Classic ist gestartet, die Synchronisierung ist an (laut Adobe-Nutzern
  werden die Originale beim Start von Classic geladen, siehe
  [Anleitung](https://lightroomkillertips.com/tips-for-syncing-lightroom-classic-with-lightroom-for-mobile/)).
- Dein Dienst legt Lightroom-Mobile-Fotos wirklich in die Inbox. Der Code der App kennt die Quelle `lightroom-mobile`
  (Pfad `/mobile-inbox/`), ob **dein** Dienst das tut, weiß ich nicht.
- Die Originale müssen vom Handy über das Schiffs-WLAN in die Cloud. Das kann bei vielen Fotos sehr langsam sein.

**Weg B: Die MHPhotos-App auf dem iPhone allein.** Aus Lightroom Mobile exportieren oder teilen, in der MHPhotos-App
"Neues Foto": Die App kann KI-Vorschläge, Kategorien, Veröffentlichen und Instagram-Planung. Das funktioniert ohne Mac
und ohne mich. Ich kann dabei helfen, wenn du mir die Fotos als Bilder in den Chat gibst (Auswahl, Bildtexte,
Bearbeitungsvorschläge als Text für Lightroom Mobile), aber ich kann sie dann nicht selbst in die App übernehmen.

Empfehlung: **Weg B als Grundlage**, weil er nicht von deinem Mac abhängt. **Weg A zusätzlich**, wenn der Mac läuft
und dein Dienst die Fotos in die Inbox bringt, denn nur dann kann ich auswählen, bearbeiten, veröffentlichen und planen.

## Wie du mit mir unterwegs sprichst
Claude Code im Web oder in der Claude-App auf dem Handy, Sitzung auf dem Repository `mhphotos-lightroom-mcp`. Die schweren
Daten (Fotos) laufen zwischen der Cloud-Sitzung und MHPhotos, über das Schiffs-WLAN gehen nur Textnachrichten.
In der Sitzung nutze ich `mhphotos-lightroom-mcp call ...` (MCP-Verbindung ist nicht nötig):

```
mhphotos-lightroom-mcp call mhphotos_inbox '{"status":"wartet"}'
mhphotos-lightroom-mcp call mhphotos_inbox_contact_sheet '{"item_ids":["lr-1","lr-2"]}'   # Bild als Datei
mhphotos-lightroom-mcp call mhphotos_inbox_images '{"item_ids":["lr-1"]}' --out bilder
mhphotos-lightroom-mcp call mhphotos_edit_preview '{"inbox_item_id":"lr-1","adjust":{"exposure":0.3,"crop":"4:5"}}'
mhphotos-lightroom-mcp call mhphotos_analyze_inbox '{"item_ids":["lr-1"]}'
mhphotos-lightroom-mcp call mhphotos_publish '{"item_ids":["lr-1"]}'                        # Vorschau; mit "confirm":true veröffentlichen
mhphotos-lightroom-mcp call mhphotos_schedule_instagram '{"inbox_item_id":"lr-1","caption":"...","scheduled_at":"2026-10-25T18:00:00+02:00"}'
```
Nichts davon ändert etwas ohne `"confirm": true`. Bei `call` kann der Server dich nicht selbst per Ja/Nein fragen. Ob
dich vorher die Berechtigungsabfrage deiner Claude-App fragt, hängt vom Berechtigungsmodus der Sitzung ab (im
automatischen Modus fragt sie nicht). Wer das strenger will, setzt `MHPHOTOS_CONFIRMATION=require`: dann verweigert
`call` jede bestätigte Änderung, und ich zeige dir stattdessen nur die Vorschau.

## Einrichtung in der Cloud-Umgebung (einmalig, vor der Abreise)
In den Einstellungen der Cloud-Umgebung (Menü in der Titelleiste der Sitzung, dann Bearbeiten):
1. **Netzwerk:** deine MHPhotos-Domain als erlaubten Host eintragen (und "Paketmanager erlauben" angehakt lassen).
   Für `mhphotos_instagram_images` zusätzlich `*.cdninstagram.com` und `*.fbcdn.net`.
2. **Geheimnisse:** `MHPHOTOS_ADMIN_TOKEN` als Umgebungsvariable beziehungsweise Netzwerk-Geheimnis,
   dazu `MHPHOTOS_INBOX_URL=https://<deine Domain>`. Den Token nie in den Chat schreiben.
3. **Freigaben nur bei Bedarf:** `MHPHOTOS_ALLOW_PUBLISH=1` (Veröffentlichen auf der Seite) und `MHPHOTOS_ALLOW_INSTAGRAM=1`
   (Instagram planen). Ohne diese Variablen kann ich nur ansehen und vorschlagen.
4. In einer neuen Sitzung installiere ich das Paket selbst (`pip install -e .`).

## Probelauf vor der Abreise (30 Minuten)
1. Ein Probe-Foto in die Inbox bringen (Weg A) oder per Plugin: `send_to_mhphotos_inbox`.
2. Neue Cloud-Sitzung: `call mhphotos_inbox` muss das Foto zeigen, `call mhphotos_inbox_images` es als Datei liefern.
3. `mhphotos_schedule_instagram` mit **Konto `mhphotos`**, einem Zeitpunkt in 30 Minuten und Vorschau, dann bestätigen und in der
   Warteschlange (`mhphotos_instagram_queue`) prüfen, dann **wieder abbrechen** (`mhphotos_cancel_instagram`), falls nichts
   posten soll. So weißt du, dass der Weg wirklich funktioniert.
4. Läuft das nicht, bleibt Weg B.

## Sicherheit für Beiträge, während du nicht zuschaust
- Instagram-Beiträge erscheinen **automatisch** zur geplanten Zeit. Plane immer mit Vorlauf und lass dir die Warteschlange zeigen.
- Das Standard-Konto ist `mhphotos`. `malou` und `martin` (privat) nur, wenn du sie ausdrücklich nennst.
- Der MHPhotos-Duplikat-Check läuft vor jedem Beitrag; ein bereits geplantes oder gepostetes Foto wird abgelehnt.
- Alle Änderungen stehen im lokalen Änderungsprotokoll (`list_changes`); in der Cloud-Sitzung liegt es nur für die Dauer der Sitzung.
