#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Version aus zentraler Quelle (punchbuddy/version.py) ─────────────────────
PB_VERSION=$(python3 -c "import sys;sys.path.insert(0,'$SCRIPT_DIR');from punchbuddy.version import __version__;print(__version__)")
echo "PunchBuddy-Version: v$PB_VERSION"
cd "$SCRIPT_DIR"

echo "=== Alte Build-Artefakte entfernen ==="
mv build /tmp/old_build_$$ 2>/dev/null || true
mv dist /tmp/old_dist_$$ 2>/dev/null || true
mv PunchBuddy.dmg /tmp/old_dmg_$$ 2>/dev/null || true
mv PunchBuddy.spec /tmp/old_spec_$$ 2>/dev/null || true
mv Diagnose.spec /tmp/old_diag_$$ 2>/dev/null || true
rm -rf /tmp/build_ap_arm64 /tmp/build_ap_x86_64
rm -rf /tmp/dist_arm64 /tmp/dist_x86_64
rm -rf /tmp/dist_ap
export PYINSTALLER_CONFIG_DIR="/tmp/pyi_config_$$"

# Python interpreter detection (ARM64 / Native)
if [ -d "$SCRIPT_DIR/../venv" ]; then
  PYTHON_ARM64="$SCRIPT_DIR/../venv/bin/python3"
elif [ -d "$HOME/.gemini/antigravity/scratch/venv" ]; then
  PYTHON_ARM64="$HOME/.gemini/antigravity/scratch/venv/bin/python3"
else
  PYTHON_ARM64="python3"
fi

# Python interpreter detection (x86_64 / Intel)
if [ -d "$SCRIPT_DIR/../venv_x86_64" ]; then
  PYTHON_X86_64="$SCRIPT_DIR/../venv_x86_64/bin/python3"
elif [ -d "$HOME/.gemini/antigravity/scratch/venv_x86_64" ]; then
  PYTHON_X86_64="$HOME/.gemini/antigravity/scratch/venv_x86_64/bin/python3"
else
  PYTHON_X86_64="python3"
fi

echo "ARM64 Python: $PYTHON_ARM64"
echo "x86_64 Python: $PYTHON_X86_64"

# ─────────────────────────────────────────────────────────────────────────────
# 1. ARM64 BUILD
# ─────────────────────────────────────────────────────────────────────────────
echo "========================================="
echo "=== BUILD 1/2: ARM64 (Apple Silicon) ==="
echo "========================================="

echo "=== PyInstaller Build (ARM64): PunchBuddy ==="
"$PYTHON_ARM64" -m PyInstaller \
  --name="PunchBuddy" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_ap_arm64" \
  --distpath="/tmp/dist_arm64" \
  --specpath="/tmp/spec_ap" \
  --target-arch=arm64 \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  --add-data="$SCRIPT_DIR/PunchBuddy.png:." \
  --add-data="$SCRIPT_DIR/PunchBuddy_Anleitung.html:." \
  --add-data="$SCRIPT_DIR/PunchBuddy_Technische_Doku.html:." \
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

echo "=== PyInstaller Build (ARM64): PunchBuddy Diagnose ==="
"$PYTHON_ARM64" -m PyInstaller \
  --name="PunchBuddy_Diagnose" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_ap_arm64" \
  --distpath="/tmp/dist_arm64" \
  --specpath="/tmp/spec_ap" \
  --target-arch=arm64 \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  collect_diagnostics.py

echo "=== PyInstaller Build (ARM64): Watchdog ==="
"$PYTHON_ARM64" -m PyInstaller \
  --name="PunchBuddy_Watchdog" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_ap_arm64" \
  --distpath="/tmp/dist_arm64" \
  --specpath="/tmp/spec_ap" \
  --target-arch=arm64 \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  --hidden-import=rumps \
  watchdog.py


# ─────────────────────────────────────────────────────────────────────────────
# 2. x86_64 BUILD
# ─────────────────────────────────────────────────────────────────────────────
echo "========================================="
echo "=== BUILD 2/2: x86_64 (Intel Mac)     ==="
echo "========================================="

echo "=== PyInstaller Build (x86_64): PunchBuddy ==="
arch -x86_64 "$PYTHON_X86_64" -m PyInstaller \
  --name="PunchBuddy" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_ap_x86_64" \
  --distpath="/tmp/dist_x86_64" \
  --specpath="/tmp/spec_ap" \
  --target-arch=x86_64 \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  --add-data="$SCRIPT_DIR/PunchBuddy.png:." \
  --add-data="$SCRIPT_DIR/PunchBuddy_Anleitung.html:." \
  --add-data="$SCRIPT_DIR/PunchBuddy_Technische_Doku.html:." \
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

echo "=== PyInstaller Build (x86_64): PunchBuddy Diagnose ==="
arch -x86_64 "$PYTHON_X86_64" -m PyInstaller \
  --name="PunchBuddy_Diagnose" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_ap_x86_64" \
  --distpath="/tmp/dist_x86_64" \
  --specpath="/tmp/spec_ap" \
  --target-arch=x86_64 \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  collect_diagnostics.py

echo "=== PyInstaller Build (x86_64): Watchdog ==="
arch -x86_64 "$PYTHON_X86_64" -m PyInstaller \
  --name="PunchBuddy_Watchdog" \
  --windowed \
  --noconfirm \
  --workpath="/tmp/build_ap_x86_64" \
  --distpath="/tmp/dist_x86_64" \
  --specpath="/tmp/spec_ap" \
  --target-arch=x86_64 \
  --icon="$SCRIPT_DIR/PunchBuddy.icns" \
  --hidden-import=rumps \
  watchdog.py


# ─────────────────────────────────────────────────────────────────────────────
# 3. MERGE TO UNIVERSAL2
# ─────────────────────────────────────────────────────────────────────────────
echo "========================================="
echo "=== MERGING TO UNIVERSAL2 BUNDLES     ==="
echo "========================================="

mkdir -p /tmp/dist_ap

"$PYTHON_ARM64" merge_universal.py \
  /tmp/dist_arm64/PunchBuddy.app \
  /tmp/dist_x86_64/PunchBuddy.app \
  /tmp/dist_ap/PunchBuddy.app

"$PYTHON_ARM64" merge_universal.py \
  /tmp/dist_arm64/PunchBuddy_Diagnose.app \
  /tmp/dist_x86_64/PunchBuddy_Diagnose.app \
  /tmp/dist_ap/PunchBuddy_Diagnose.app

"$PYTHON_ARM64" merge_universal.py \
  /tmp/dist_arm64/PunchBuddy_Watchdog.app \
  /tmp/dist_x86_64/PunchBuddy_Watchdog.app \
  /tmp/dist_ap/PunchBuddy_Watchdog.app


# ── 4. Alle Apps signieren ───────────────────────────────────────────────
echo "=== Ad-hoc Code-Signaturen (Universal2) ==="
# Version VOR dem Signieren in die Bundles schreiben (plutil nach codesign
# bricht das Signatur-Siegel -> TCC verweigert Automation; Bug v2.0.0)
for _APP in /tmp/dist_ap/PunchBuddy.app /tmp/dist_ap/PunchBuddy_Diagnose.app /tmp/dist_ap/PunchBuddy_Watchdog.app; do
  plutil -replace CFBundleShortVersionString -string "$PB_VERSION" "$_APP/Contents/Info.plist"
  plutil -replace CFBundleVersion            -string "$PB_VERSION" "$_APP/Contents/Info.plist"
done
codesign --force --deep --sign - /tmp/dist_ap/PunchBuddy.app
codesign --force --deep --sign - /tmp/dist_ap/PunchBuddy_Diagnose.app
codesign --force --deep --sign - /tmp/dist_ap/PunchBuddy_Watchdog.app

# ── 5. DMG-Staging-Ordner mit allen Apps erstellen ───────────────────────
echo "=== DMG vorbereiten ==="
DMG_STAGE="/tmp/dist_ap/dmg_stage"
rm -rf "$DMG_STAGE"
mkdir -p "$DMG_STAGE"
ditto /tmp/dist_ap/PunchBuddy.app "$DMG_STAGE/PunchBuddy.app"
ditto /tmp/dist_ap/PunchBuddy_Diagnose.app "$DMG_STAGE/PunchBuddy_Diagnose.app"
ditto /tmp/dist_ap/PunchBuddy_Watchdog.app "$DMG_STAGE/PunchBuddy_Watchdog.app"
cp    "$SCRIPT_DIR/PunchBuddy_Setup.command" "$DMG_STAGE/"
chmod +x "$DMG_STAGE/PunchBuddy_Setup.command"
cp    "$SCRIPT_DIR/Anti_AppNap.command"        "$DMG_STAGE/"
chmod +x "$DMG_STAGE/Anti_AppNap.command"
osacompile -o "$DMG_STAGE/Anti_AppNap.app" "$SCRIPT_DIR/Anti_AppNap.applescript"
cp "$SCRIPT_DIR/Anti_AppNap.icns" "$DMG_STAGE/Anti_AppNap.app/Contents/Resources/applet.icns"
# Dokumentation mitkopieren (falls vorhanden)
mkdir -p "$DMG_STAGE/Anleitungen"
cp "$SCRIPT_DIR"/PunchBuddy_Anleitung*.html       "$DMG_STAGE/Anleitungen/" 2>/dev/null || true
cp "$SCRIPT_DIR"/PunchBuddy_Technische_Doku*.html "$DMG_STAGE/Anleitungen/" 2>/dev/null || true

# Anti_AppNap-Applet: Version setzen, dann signieren (frisch kompiliert)
plutil -replace CFBundleShortVersionString -string "$PB_VERSION" "$DMG_STAGE/Anti_AppNap.app/Contents/Info.plist" 2>/dev/null || true
codesign --force --sign - "$DMG_STAGE/Anti_AppNap.app"

# ── 6. DMG erstellen ──────────────────────────────────────────────────────
echo "=== DMG erstellen ==="
rm -f "/tmp/PunchBuddy_raw.dmg"
rm -f "$SCRIPT_DIR/PunchBuddy_v${PB_VERSION}.dmg"

# 1. Hybrid Image (unkomprimiert) erstellen (umgeht Error -1 auf manchen Systemen)
hdiutil makehybrid -hfs -o "/tmp/PunchBuddy_raw.dmg" "$DMG_STAGE"

# 2. In komprimiertes UDZO Format konvertieren
hdiutil convert -format UDZO -o "$SCRIPT_DIR/PunchBuddy_v${PB_VERSION}.dmg" "/tmp/PunchBuddy_raw.dmg"

rm -f "/tmp/PunchBuddy_raw.dmg"

echo ""
echo "✅ Fertig: PunchBuddy_v${PB_VERSION}.dmg (UNIVERSAL2)"
echo "   Enthält: PunchBuddy.app + PunchBuddy_Diagnose.app + PunchBuddy_Watchdog.app + PunchBuddy_Setup.command"
echo "            + PunchBuddy_Anleitung.html + PunchBuddy_Technische_Doku.html"
