#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# make_launchers.command
#
# Erzeugt für jeden PunchBuddy-Befehl ein winziges macOS-.app, das den Befehl
# über den lokalen Unix-Socket an PunchBuddy schickt – KOMPLETT OHNE NETZWERK.
#
# Diese .apps werden im Stream Deck mit der eingebauten Aktion
#   "System  →  Öffnen"  (Open)
# als zu öffnende Anwendung ausgewählt. Ein Tastendruck = ein Befehl.
#
# Doppelklick auf diese Datei genügt. Optionaler Zielordner als 1. Argument.
# Socket-Pfad via Umgebungsvariable PUNCHBUDDY_SOCK überschreibbar.
# ─────────────────────────────────────────────────────────────────────────────
set -e

SOCK="${PUNCHBUDDY_SOCK:-/tmp/punchbuddy.sock}"
DEST="${1:-$HOME/Applications/PunchBuddy Launchers}"

echo "PunchBuddy Stream-Deck-Launcher werden erzeugt"
echo "  Socket : $SOCK"
echo "  Ziel   : $DEST"
echo

mkdir -p "$DEST"

# sanitize: erzeugt aus einem Namen einen gültigen Bundle-ID-/Datei-Baustein
slug() { printf '%s' "$1" | tr 'A-Z ' 'a-z-' | tr -cd 'a-z0-9-'; }

make_app() {
    appname="$1"   # Dateiname der .app (ohne .app)
    label="$2"     # Anzeigename
    payload="$3"   # exakter Befehl, der an den Socket geht (z. B. "preset 3")

    app="$DEST/$appname.app"
    id="com.punchbuddy.launcher.$(slug "$appname")"
    rm -rf "$app"
    mkdir -p "$app/Contents/MacOS"

    cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>CFBundleName</key><string>$appname</string>
    <key>CFBundleDisplayName</key><string>$label</string>
    <key>CFBundleIdentifier</key><string>$id</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>LSBackgroundOnly</key><true/>
    <key>LSUIElement</key><true/>
</dict></plist>
PLIST

    # Launcher-Skript: sendet den Befehl; bei Fehler kurze Notification.
    cat > "$app/Contents/MacOS/launcher" <<LAUNCH
#!/bin/sh
printf '%s' '$payload' | /usr/bin/nc -U '$SOCK' >/dev/null 2>&1 || \\
/usr/bin/osascript -e 'display notification "PunchBuddy nicht erreichbar – läuft es?" with title "PunchBuddy"' >/dev/null 2>&1
LAUNCH
    chmod +x "$app/Contents/MacOS/launcher"
    echo "  ✓ $appname.app  →  '$payload'"
}

# ── Transport / Aufnahme ────────────────────────────────────────────────────
make_app "PunchBuddy-RecordA"    "Record A"          "record_a"
make_app "PunchBuddy-RecordB"    "Record B"          "record_b"
make_app "PunchBuddy-Play"       "Play / Stop"       "play"
make_app "PunchBuddy-PlayCustom" "Play Custom"       "play_custom"
make_app "PunchBuddy-GotoStart"  "Cursor an Start"   "goto_start"
make_app "PunchBuddy-MoveAudio"  "Audio verschieben" "move_audio"

# ── Import / Export ─────────────────────────────────────────────────────────
make_app "PunchBuddy-Import"           "Import"             "import"
make_app "PunchBuddy-ExportWav"        "Export WAV"         "export_wav"
make_app "PunchBuddy-ExportAaf"        "Export AAF"         "export_aaf"
make_app "PunchBuddy-ExportAafRef"     "Export AAF Ref"     "export_aaf_reference"
make_app "PunchBuddy-ExportInterplay"  "Export Interplay"   "export_interplay"

# ── Presets 1–8 ─────────────────────────────────────────────────────────────
i=1
while [ "$i" -le 8 ]; do
    make_app "PunchBuddy-Preset$i" "Preset $i" "preset $i"
    i=$((i + 1))
done

# ── Vocaster ────────────────────────────────────────────────────────────────
make_app "PunchBuddy-VocAutogainHost"  "Vocaster Autogain Host"  "vocaster_autogain_host"
make_app "PunchBuddy-VocAutogainGuest" "Vocaster Autogain Guest" "vocaster_autogain_guest"
make_app "PunchBuddy-VocPhantomOn"     "Vocaster Phantom AN"     "vocaster_phantom_on"
make_app "PunchBuddy-VocPhantomOff"    "Vocaster Phantom AUS"    "vocaster_phantom_off"

echo
echo "Fertig. $(ls "$DEST" | grep -c '\.app$') Launcher in:"
echo "  $DEST"
echo
echo "Im Stream Deck:  Aktion 'System → Öffnen' auf eine Taste ziehen,"
echo "dann unter 'App / Datei' den passenden Launcher auswählen."
echo
# Ordner im Finder zeigen, wenn interaktiv gestartet.
[ -t 1 ] && /usr/bin/open "$DEST" 2>/dev/null || true
