# PunchBuddy ↔ Stream Deck (ohne Netzwerk)

Diese beiden Wege steuern PunchBuddy **komplett ohne Netzwerk** – über den
lokalen **Unix-Domain-Socket** (`/tmp/punchbuddy.sock`). Kein TCP, kein
Loopback, kein offener Port → für Netzwerk-Filter wie **Microsoft Defender**
prinzipiell **nicht erfassbar**.

> Voraussetzung: In PunchBuddy muss die Unix-Socket-Steuerung aktiv sein
> (Standard: an). Einstellung `unix_socket_enabled` / `unix_socket_path` in der
> `settings.json`. Beim Start schreibt PunchBuddy ins Log:
> `Unix-Socket-Steuerung aktiv: /tmp/punchbuddy.sock`.

Beide Wege bedienen **dieselbe zentrale Befehls-API** wie der HTTP-Webtrigger.
Befehlsliste jederzeit abrufbar mit: `printf 'list' | nc -U /tmp/punchbuddy.sock`.

| Befehl | Wirkung |
|---|---|
| `record_a`, `record_b` | Aufnahme Profil A / B |
| `play`, `play_custom` | Play/Stop, Play Custom |
| `goto_start` | Cursor an Start-Timecode |
| `move_audio` | Audio Quell- → Ziel-Spuren |
| `import` | Interplay-Import |
| `export_wav`, `export_aaf`, `export_aaf_reference`, `export_interplay` | Exporte |
| `preset 1` … `preset 8` | Preset laden |
| `vocaster_autogain_host` / `_guest`, `vocaster_phantom_on` / `_off` | Vocaster |
| `ping`, `status`, `list` | Diagnose |

---

## Weg A — `.app`-Launcher  (empfohlen: keine Installation, kein Build)

Für jeden Befehl eine winzige `.app`, die den Befehl an den Socket schickt. Im
Stream Deck wird die eingebaute Aktion **„System → Öffnen"** auf diese App
gelegt. Funktioniert mit **jeder** Stream-Deck-Version, ohne Plugin-Installation.

### Einrichten
1. **Launcher erzeugen:** Doppelklick auf [`make_launchers.command`](make_launchers.command).
   Erzeugt alle Launcher in `~/Applications/PunchBuddy Launchers/`.
   (Anderer Socket-Pfad? `PUNCHBUDDY_SOCK=/pfad.sock` davor setzen.)
2. **Im Stream Deck:** Aktion **System → Öffnen** auf eine Taste ziehen.
3. Unter **App/Datei** den passenden Launcher wählen
   (z. B. `PunchBuddy-Play.app`), Tastentitel/Icon nach Wunsch setzen.

### Direkt aus dem Terminal / Keyboard Maestro / Skripten
[`punchbuddy-send`](punchbuddy-send) ist ein eigenständiges CLI:
```sh
./punchbuddy-send play
./punchbuddy-send preset 3
./punchbuddy-send status
```

**Vorteile:** null Abhängigkeiten, kein Build, robust, jede SD-Version.
**Nachteil:** pro Befehl eine eigene App/Taste; ~150–300 ms App-Start-Latenz.

---

## Weg B — Echtes Stream-Deck-Plugin  (eine Aktion, Befehl pro Taste wählbar)

Ein natives Node-Plugin: **eine** Aktion „PunchBuddy Befehl", bei der man pro
Taste im Property-Inspector den gewünschten Befehl aus einem Dropdown wählt.
Verbindet sich direkt über Node `net` mit dem Unix-Socket. Schönere Bedienung,
weniger Tasten-Verwaltung. Details und Build-Anleitung: [`plugin/README.md`](plugin/README.md).

**Vorteil:** eine Aktion für alles, Dropdown-Auswahl, Erfolg/Fehler-Feedback
(✓ / ⚠ auf der Taste).
**Nachteil:** einmaliger Build (Node/npm) und Stream Deck ≥ 6.5 nötig.

---

## Sicherheit
Der Socket hat Datei-Rechte **0600** – es kann **nur der angemeldete Benutzer**
verbinden. Es ist kein Token nötig (anders als beim HTTP-Webtrigger im LAN).

## Fehlersuche
- `printf 'ping' | nc -U /tmp/punchbuddy.sock` → erwartet `OK pong`.
- Kommt `nc: ... No such file or directory`: PunchBuddy läuft nicht oder
  Unix-Socket ist deaktiviert.
- Anderer Socket-Pfad in den Einstellungen? Dann Launcher mit passendem
  `PUNCHBUDDY_SOCK` neu erzeugen bzw. im Plugin das Feld „Socket" setzen.
