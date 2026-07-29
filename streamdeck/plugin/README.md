# PunchBuddy – Stream-Deck-Plugin

Natives Elgato-Stream-Deck-Plugin (Node.js). **Eine** Aktion „PunchBuddy
Befehl"; pro Taste wählt man im Property-Inspector den Befehl aus einem
Dropdown. Der Tastendruck öffnet den **lokalen Unix-Socket** von PunchBuddy
(`/tmp/punchbuddy.sock`, via Node `net`) und sendet den Befehl – **kein
Netzwerk**. Die Taste zeigt ✓ (OK) bzw. ⚠ (Fehler) als Quittung.

## Voraussetzungen
- **Stream Deck App ≥ 6.5** (Node-Plugin-Runtime).
- Zum **Bauen**: Node.js + npm (nur einmalig; das fertige Bundle ist
  self-contained).
- PunchBuddy läuft mit aktiver Unix-Socket-Steuerung.

## Bauen & Packen
Doppelklick auf [`build.command`](build.command) – oder:
```sh
npm install
npm run build      # esbuild bündelt src/plugin.js -> .sdPlugin/bin/plugin.js (inkl. ws)
sh pack.sh         # erzeugt com.punchbuddy.control.streamDeckPlugin
```

## Installieren
- **Einfach:** Doppelklick auf `com.punchbuddy.control.streamDeckPlugin`
  → Stream Deck installiert es.
- **Manuell (Entwicklung):** Ordner `com.punchbuddy.control.sdPlugin` nach
  `~/Library/Application Support/com.elgato.StreamDeck/Plugins/` kopieren und
  Stream Deck neu starten.

## Benutzen
1. Aktion **PunchBuddy → PunchBuddy Befehl** auf eine Taste ziehen.
2. Im Property-Inspector den **Befehl** wählen (bei „Preset laden …" zusätzlich
   die **Preset-Nr.**).
3. Optional **Socket**-Pfad setzen (leer = `/tmp/punchbuddy.sock`).

## Aufbau
```
plugin/
  src/plugin.js                         Quelle (Node: ws + net)
  make_icons.py                         erzeugt die Icon-PNGs (stdlib, ohne PIL)
  package.json / build.command / pack.sh  Build & Pack
  com.punchbuddy.control.sdPlugin/
    manifest.json                       Plugin-Manifest (SDKVersion 2, Nodejs 20)
    bin/plugin.js                       gebündelte Laufzeit (Build-Ergebnis)
    ui/inspector.html                   Property-Inspector (Befehls-Dropdown)
    imgs/...                            Icons
```

## Wie es funktioniert (Protokoll)
Stream Deck startet `node bin/plugin.js -port <p> -pluginUUID <id>
-registerEvent <ev> -info <json>`. Das Plugin registriert sich per WebSocket
bei der Stream-Deck-App und lauscht auf `keyDown`. Bei Tastendruck:
`net.createConnection({path: socket})` → schreibt `"<befehl>\n"` → liest die
Antwort (`OK …` / `ERR …`) → `showOk` bzw. `showAlert`.

> Getestet: Das gebündelte `plugin.js` wurde gegen den echten
> PunchBuddy-Unix-Socket end-to-end verifiziert (Registrierung → keyDown →
> Socket-Write → OK → showOk). Das Laden im echten Stream Deck (Manifest/Icons/
> Node-Runtime) bitte einmal am Zielrechner gegenprüfen.
