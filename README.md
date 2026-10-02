# PunchBuddy

Menüleisten-App für macOS, die wiederkehrende Handgriffe in **Avid Pro Tools**
auf einen Tastendruck legt: Punch-In-Aufnahme, Transport, Spur-Umzüge, Exporte
und Lautheits-Normalisierung. Gesteuert per Hotkey, Stream Deck oder HTTP —
gedacht für Sprecher-/Vertonungsplätze, an denen dieselbe Abfolge hundertmal am
Tag läuft.

Steuerung von Pro Tools über **PTSL** (Pro Tools Scripting Layer, `py-ptsl`),
ergänzt um CGEvent-Tastendrücke für die wenigen Funktionen, die PTSL nicht
anbietet.

Entwickelt von Jens Rühl. Version 2.1.1.

## Download

Fertiges DMG im [Release v2.1.1](https://github.com/ruehljens-creator/punchbuddy-protools/releases/tag/v2.1.1) —
`PunchBuddy_v2.1.1_Intel.dmg` für Intel-Macs. Ein Apple-Silicon-DMG für 2.1.1 folgt; bis dahin gibt es für
M1/M2/M3/M4 `PunchBuddy_v2.0.2_AppleSilicon.dmg` im [Release v2.0.2](https://github.com/ruehljens-creator/punchbuddy-protools/releases/tag/v2.0.2)
(ohne die Neuerungen aus 2.1.0 und 2.1.1).
Die DMGs bringen libusb und das Stream-Deck-Plugin mit, Homebrew wird nicht gebraucht.

Die Apps sind ad-hoc signiert, nicht notarisiert: beim ersten Start **Rechtsklick → „Öffnen"**.

## Was die App macht

- **Punch-In-Automation** — Ziel-Spuren finden, Record-Enable an, Input-Monitor
  aus, Pre-Roll an, Aufnahme starten; beim Stop läuft alles sauber zurück
  (inkl. Post-Roll-Abwarten). Zwei getrennte Profile A/B.
- **Transport** — Play, Play Custom, Stop, Cursor an Start-Timecode.
- **Spur-Umzüge** — Audio von Quell- auf Ziel-Spuren verschieben.
- **Exporte** — WAV, AAF, AAF mit Referenz, Interplay-Import/-Export.
- **Lautheit** — Normalisierung nach EBU R128 (`pyloudnorm`), mit
  Fortschrittsfenster.
- **Presets** — bis zu acht komplette Konfigurationen, per Taste umschaltbar.
- **Vocaster-Integration** — Focusrite Vocaster Two direkt über das
  Scarlett2-USB-Protokoll (Auto-Gain Host/Gast, Phantomspeisung).
- **Watchdog** — separater Prozess, der die App überwacht und neu starten kann.
- **Diagnose** — `collect_diagnostics.py` sammelt einen Zustandsbericht des
  Rechners für die Fehlersuche.
- Oberfläche und Anleitungen in fünf Sprachen (de/en/es/fr/pt).

## Steuerwege

| Weg | Netzwerk? | Wofür |
|---|---|---|
| Globaler Hotkey | — | schnellster Weg am Platz |
| **Unix-Domain-Socket** `/tmp/punchbuddy.sock` | nein | Stream Deck, CLI, Keyboard Maestro — Dateirechte 0600, nur der angemeldete Benutzer |
| HTTP-Webtrigger | ja | Steuerung aus dem LAN; beim Binden auf `0.0.0.0` wird automatisch ein Token erzwungen (`?token=…` bzw. `X-Auth-Token`) |
| Stream-Deck-Plugin | nein | eine Aktion, Befehl pro Taste im Dropdown |

Details zu Stream Deck: [`streamdeck/README.md`](streamdeck/README.md), Plugin-Build:
[`streamdeck/plugin/README.md`](streamdeck/plugin/README.md).

## Installation aus dem Quellcode

Voraussetzungen: macOS, Python 3.9+, laufendes Pro Tools mit aktiviertem
PTSL-Zugriff.

```bash
pip install -r requirements.txt
python3 auto_punch_in.py
```

Beim ersten Start fragt macOS nach den Rechten für **Bedienungshilfen** und
**Automation** (nötig für die Tastendruck-Injektion). Ein fertiges `.app`/DMG
baut `build_dmg.sh` (Apple Silicon) bzw. `build_dmg_intel.sh`.

Empfohlen: `Anti_AppNap.command` einmal ausführen — macOS drosselt sonst
Hintergrund-Prozesse und die Reaktionszeiten werden unzuverlässig.

## Aufbau

| Datei / Ordner | Inhalt |
|---|---|
| `auto_punch_in.py` | Einstiegspunkt, Menüleiste, Einstellungsfenster |
| `punchbuddy/engine.py` | PTSL-Verbindung: Singleton, gRPC-Deadline, serialisierte Aufrufe, Track-Cache |
| `punchbuddy/transport.py` | Punch-In/Out, Play, Stop, GoTo |
| `punchbuddy/export.py` | WAV-/AAF-/Interplay-Workflows |
| `punchbuddy/loudness.py` | EBU-R128-Normalisierung |
| `punchbuddy/keys.py` | CGEvent-Tastendrücke, PID-Ermittlung |
| `punchbuddy/config.py` · `state.py` · `log.py` · `i18n.py` · `uikit.py` | Einstellungen, Laufzeitzustand, Logging, Übersetzungen, AppKit-Helfer |
| `streamdeck/` | Launcher, CLI `punchbuddy-send`, Stream-Deck-Plugin |
| `tools/pt_mcp_server.py` | MCP-Server: lesende Pro-Tools-Abfragen für KI-Werkzeuge |
| `PunchBuddy_Anleitung*.html` | Bedienungsanleitung (5 Sprachen) |
| `PunchBuddy_Technische_Doku*.html` | technische Dokumentation (5 Sprachen) |
| `tests/`, `test_*.py` | Tests und Einzelversuche zu Hotkeys, PTSL, Auswahl |

Die Module importieren strikt nach „unten" (i18n/log/state → keys → engine →
transport/export → UI), damit es keine Zirkularimporte gibt.

## Lizenz

[MIT](LICENSE).
