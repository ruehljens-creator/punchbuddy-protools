#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Intel-only DMG-Build (x86_64) – KEIN Universal-Merge.
# Bündelt libusb-1.0 mit, damit die Vocaster-Steuerung ohne Homebrew läuft.
# Ergebnis: PunchBuddy_v${PB_VERSION}_Intel.dmg
# ─────────────────────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ── Version aus zentraler Quelle (punchbuddy/version.py) ─────────────────────
PB_VERSION=$(python3 -c "import sys;sys.path.insert(0,'$SCRIPT_DIR');from punchbuddy.version import __version__;print(__version__)")
echo "PunchBuddy-Version: v$PB_VERSION"

# ── libusb-Quelle finden (x86_64) ────────────────────────────────────────────
LIBUSB=""
for cand in /usr/local/lib/libusb-1.0.0.dylib /usr/local/opt/libusb/lib/libusb-1.0.0.dylib; do
  if [ -f "$cand" ]; then LIBUSB="$cand"; break; fi
done
if [ -z "$LIBUSB" ]; then
  echo "❌ libusb-1.0.0.dylib (x86_64) nicht gefunden. Bitte: brew install libusb"
  exit 1
fi
# Architektur prüfen
if ! file "$LIBUSB" | grep -q "x86_64"; then
  echo "❌ $LIBUSB ist nicht x86_64:"; file "$LIBUSB"; exit 1
fi
echo "libusb (x86_64): $LIBUSB"

# ── Python (x86_64) ──────────────────────────────────────────────────────────
if [ -d "$SCRIPT_DIR/../venv_x86_64" ]; then
  PYTHON_X86_64="$SCRIPT_DIR/../venv_x86_64/bin/python3"
elif [ -x "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3" ]; then
  # Framework-Python (x86_64 mit PyInstaller + rumps/ptsl/etc.)
  PYTHON_X86_64="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
else
  PYTHON_X86_64="python3"
fi
echo "x86_64 Python: $PYTHON_X86_64"

export PYINSTALLER_CONFIG_DIR="/tmp/pyi_config_intel_$$"
rm -rf /tmp/build_intel /tmp/dist_intel

# ─────────────────────────────────────────────────────────────────────────────
# 1. PunchBuddy (x86_64) – mit gebündelter libusb
# ─────────────────────────────────────────────────────────────────────────────
echo "=== PyInstaller (x86_64): PunchBuddy ==="
# Alle Doku-HTMLs inkl. Sprachvarianten einsammeln (PunchBuddy_Anleitung*.html,
# PunchBuddy_Technische_Doku*.html) – so werden neue Sprachen automatisch gebündelt.
DOC_DATA=()
for f in "$SCRIPT_DIR"/PunchBuddy_Anleitung*.html "$SCRIPT_DIR"/PunchBuddy_Technische_Doku*.html; do
  [ -f "$f" ] && DOC_DATA+=( --add-data="$f:." )
done
echo "  Gebündelte Doku-Dateien: ${#DOC_DATA[@]}"
arch -x86_64 "$PYTHON_X86_64" -m PyInstaller \
  --name="PunchBuddy" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_intel" \
  --distpath="/tmp/dist_intel" \
  --specpath="/tmp/spec_intel" \
  --target-arch=x86_64 \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  --add-data="$SCRIPT_DIR/PunchBuddy.png:." \
  "${DOC_DATA[@]}" \
  --add-data="$SCRIPT_DIR/streamdeck/plugin/com.punchbuddy.control.streamDeckPlugin:streamdeck" \
  --add-binary="$LIBUSB:." \
  --hidden-import=Foundation \
  --hidden-import=AppKit \
  --hidden-import=objc \
  --hidden-import=Quartz \
  --hidden-import=rumps \
  --hidden-import=ptsl \
  --hidden-import=http.server \
  --hidden-import=socketserver \
  --hidden-import=pyloudnorm \
  --hidden-import=soundfile \
  --hidden-import=numpy \
  --collect-submodules punchbuddy \
  --paths "$SCRIPT_DIR" \
  auto_punch_in.py

echo "=== PyInstaller (x86_64): PunchBuddy Diagnose ==="
arch -x86_64 "$PYTHON_X86_64" -m PyInstaller \
  --name="PunchBuddy_Diagnose" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_intel" \
  --distpath="/tmp/dist_intel" \
  --specpath="/tmp/spec_intel" \
  --target-arch=x86_64 \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  collect_diagnostics.py

echo "=== PyInstaller (x86_64): Watchdog ==="
arch -x86_64 "$PYTHON_X86_64" -m PyInstaller \
  --name="PunchBuddy_Watchdog" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_intel" \
  --distpath="/tmp/dist_intel" \
  --specpath="/tmp/spec_intel" \
  --target-arch=x86_64 \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  --hidden-import=rumps \
  watchdog.py

# ── Version in die App-Bundles schreiben (VOR dem Signieren!) ────────────────
# Reihenfolge ist entscheidend: plutil nach codesign bricht das Signatur-
# Siegel -> TCC verweigert Automation/Bedienungshilfen (Bug v2.0.0).
for _APP in /tmp/dist_intel/PunchBuddy.app /tmp/dist_intel/PunchBuddy_Diagnose.app /tmp/dist_intel/PunchBuddy_Watchdog.app; do
  plutil -replace CFBundleShortVersionString -string "$PB_VERSION" "$_APP/Contents/Info.plist"
  plutil -replace CFBundleVersion            -string "$PB_VERSION" "$_APP/Contents/Info.plist"
done

# ── 2. Signieren (ad-hoc) ────────────────────────────────────────────────────
echo "=== Ad-hoc Code-Signaturen (x86_64) ==="
codesign --force --deep --sign - /tmp/dist_intel/PunchBuddy.app
codesign --force --deep --sign - /tmp/dist_intel/PunchBuddy_Diagnose.app
codesign --force --deep --sign - /tmp/dist_intel/PunchBuddy_Watchdog.app

# ── 3. DMG-Staging ───────────────────────────────────────────────────────────
echo "=== DMG vorbereiten ==="
DMG_STAGE="/tmp/dist_intel/dmg_stage"
rm -rf "$DMG_STAGE"; mkdir -p "$DMG_STAGE"
ditto /tmp/dist_intel/PunchBuddy.app "$DMG_STAGE/PunchBuddy.app"
ditto /tmp/dist_intel/PunchBuddy_Diagnose.app "$DMG_STAGE/PunchBuddy_Diagnose.app"
ditto /tmp/dist_intel/PunchBuddy_Watchdog.app "$DMG_STAGE/PunchBuddy_Watchdog.app"
cp    "$SCRIPT_DIR/PunchBuddy_Setup.command"   "$DMG_STAGE/"
chmod +x "$DMG_STAGE/PunchBuddy_Setup.command"
cp    "$SCRIPT_DIR/Anti_AppNap.command"        "$DMG_STAGE/"
chmod +x "$DMG_STAGE/Anti_AppNap.command"
osacompile -o "$DMG_STAGE/Anti_AppNap.app" "$SCRIPT_DIR/Anti_AppNap.applescript"
cp "$SCRIPT_DIR/Anti_AppNap.icns" "$DMG_STAGE/Anti_AppNap.app/Contents/Resources/applet.icns"
mkdir -p "$DMG_STAGE/Anleitungen"
cp "$SCRIPT_DIR"/PunchBuddy_Anleitung*.html       "$DMG_STAGE/Anleitungen/" 2>/dev/null || true
cp "$SCRIPT_DIR"/PunchBuddy_Technische_Doku*.html "$DMG_STAGE/Anleitungen/" 2>/dev/null || true

# Anti_AppNap-Applet: Version setzen, dann signieren (frisch kompiliert)
plutil -replace CFBundleShortVersionString -string "$PB_VERSION" "$DMG_STAGE/Anti_AppNap.app/Contents/Info.plist" 2>/dev/null || true
codesign --force --sign - "$DMG_STAGE/Anti_AppNap.app"

# ── 4. DMG erstellen ─────────────────────────────────────────────────────────
echo "=== DMG erstellen ==="
rm -f "/tmp/PunchBuddy_intel_raw.dmg"
rm -f "$SCRIPT_DIR/PunchBuddy_v${PB_VERSION}_Intel.dmg"
hdiutil makehybrid -hfs -o "/tmp/PunchBuddy_intel_raw.dmg" "$DMG_STAGE"
hdiutil convert -format UDZO -o "$SCRIPT_DIR/PunchBuddy_v${PB_VERSION}_Intel.dmg" "/tmp/PunchBuddy_intel_raw.dmg"
rm -f "/tmp/PunchBuddy_intel_raw.dmg"

echo ""
echo "✅ Fertig: PunchBuddy_v${PB_VERSION}_Intel.dmg (x86_64, mit gebündelter libusb)"
