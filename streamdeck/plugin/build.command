#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# build.command — baut und packt das PunchBuddy-Stream-Deck-Plugin.
# Doppelklickbar. Voraussetzung: Node.js + npm installiert.
#   1. npm install        (ws + esbuild)
#   2. Icons erzeugen     (make_icons.py)
#   3. esbuild-Bundle      (src/plugin.js -> bin/plugin.js, self-contained)
#   4. .streamDeckPlugin   packen
# ─────────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

echo "==> npm install"
npm install --no-audit --no-fund

echo "==> Icons"
python3 make_icons.py

echo "==> Bundle (esbuild)"
npm run build

echo "==> Pack"
/bin/sh pack.sh

echo
echo "Fertig. Installation:"
echo "  • Doppelklick auf com.punchbuddy.control.streamDeckPlugin"
echo "    ODER Ordner com.punchbuddy.control.sdPlugin nach"
echo "    ~/Library/Application Support/com.elgato.StreamDeck/Plugins/ kopieren."
