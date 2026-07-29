#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  PunchBuddy – Geführte Berechtigungs-Einrichtung
#
#  Doppelklick zum Ausführen (auch direkt aus dem DMG möglich).
#
#  WICHTIG / EHRLICH: macOS lässt es aus Sicherheitsgründen NICHT zu,
#  Datenschutz-Berechtigungen per Skript zu setzen (die System-Datenbank
#  TCC.db ist durch SIP geschützt – selbst root darf nicht schreiben).
#  Dieses Skript erledigt darum alles Automatisierbare und reduziert den
#  Rest auf „Schalter umlegen": Es kopiert die Apps nach /Programme,
#  entfernt die Quarantäne, aktiviert den Developer-Mode, startet die
#  Apps einmal (damit sie von selbst in den Listen auftauchen) und öffnet
#  anschließend die vier Datenschutz-Bereiche der Reihe nach.
# ═══════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="/Applications"

APPS=(
    "PunchBuddy.app"
    "PunchBuddy_Watchdog.app"
    "PunchBuddy_Diagnose.app"
)

# Reihenfolge der Datenschutz-Bereiche (label, settings-anchor, auto-appear?)
PANES=(
    "Festplattenvollzugriff (Full Disk Access)|Privacy_AllFiles|nein"
    "Bedienungshilfen (Accessibility)|Privacy_Accessibility|ja"
    "Eingabeüberwachung (Input Monitoring)|Privacy_ListenEvent|ja"
    "Developer Tools|Privacy_DevTools|nein"
)

ok()   { echo "  ✓  $*"; }
warn() { echo "  ⚠  $*"; }
err()  { echo "  ✗  $*"; }
step() { echo ""; echo "──── $* ────────────────────────────────"; }
pause(){ printf "       %s" "${1:-[Return zum Fortfahren] }"; read -r; }

clear
echo ""
echo "  ╔══════════════════════════════════════════════════════╗"
echo "  ║   PunchBuddy – Berechtigungen einrichten (geführt)   ║"
echo "  ╚══════════════════════════════════════════════════════╝"
echo ""
echo "  Es werden Rechte für drei Apps eingerichtet:"
echo "    • PunchBuddy        • PunchBuddy_Watchdog       • PunchBuddy_Diagnose"
echo ""
echo "  Benötigte Berechtigungen:"
echo "    • Festplattenvollzugriff   • Bedienungshilfen"
echo "    • Eingabeüberwachung       • Developer Tools"
echo ""
echo "  Hinweis: Die vier Schalter musst du am Ende selbst umlegen –"
echo "  macOS erlaubt kein automatisches Setzen. Dieses Skript macht"
echo "  alles andere und führt dich Schritt für Schritt durch."
echo ""
pause "[Return zum Starten] "

# ═══════════════════════════════════════════════════════════════════
# Schritt 1: Apps nach /Programme kopieren
# ═══════════════════════════════════════════════════════════════════
step "Schritt 1: Apps nach /Programme kopieren"
echo ""

# Admin-Rechte (für /Programme-Schreibzugriff, Developer-Mode)
echo "  Das Admin-Passwort wird für Installation + Developer-Mode benötigt."
if sudo -v 2>/dev/null; then
    ok "Admin-Berechtigung erteilt"
    ( while true; do sudo -n true; sleep 50; done ) 2>/dev/null &
    SUDO_KEEP_PID=$!
    trap "kill $SUDO_KEEP_PID 2>/dev/null" EXIT
    HAS_SUDO=true
else
    warn "Kein Admin-Zugriff – kopiere ohne sudo (klappt meist trotzdem)."
    HAS_SUDO=false
fi
echo ""

INSTALLED=()
for app in "${APPS[@]}"; do
    src="$SCRIPT_DIR/$app"
    dst="$DEST/$app"
    if [ ! -d "$src" ]; then
        # Vielleicht liegen die Apps schon in /Programme (Skript separat gestartet)
        if [ -d "$dst" ]; then
            ok "$app – bereits in /Programme"
            INSTALLED+=("$dst")
        else
            warn "$app – nicht gefunden (weder neben dem Skript noch in /Programme)"
        fi
        continue
    fi
    # Vorhandene Version ersetzen
    if $HAS_SUDO; then
        sudo rm -rf "$dst" 2>/dev/null
        sudo cp -R "$src" "$dst" 2>/dev/null
    else
        rm -rf "$dst" 2>/dev/null
        cp -R "$src" "$dst" 2>/dev/null
    fi
    if [ -d "$dst" ]; then
        ok "$app → /Programme"
        INSTALLED+=("$dst")
    else
        err "$app – Kopieren fehlgeschlagen"
    fi
done

# ═══════════════════════════════════════════════════════════════════
# Schritt 2: Quarantäne entfernen + ad-hoc signieren
# ═══════════════════════════════════════════════════════════════════
step "Schritt 2: Gatekeeper – Quarantäne entfernen"
echo ""
for dst in "${INSTALLED[@]}"; do
    xattr -dr com.apple.quarantine "$dst" 2>/dev/null
    # Ad-hoc-Signatur stabilisiert die TCC-Identität nach dem Kopieren
    codesign --force --deep --sign - "$dst" >/dev/null 2>&1
    ok "$(basename "$dst")"
done

# ═══════════════════════════════════════════════════════════════════
# Schritt 3: Developer-Mode aktivieren
# ═══════════════════════════════════════════════════════════════════
step "Schritt 3: Developer-Mode aktivieren"
echo ""
if $HAS_SUDO; then
    if sudo /usr/sbin/DevToolsSecurity -enable >/dev/null 2>&1; then
        ok "Developer-Mode aktiviert (DevToolsSecurity)"
    else
        warn "DevToolsSecurity nicht verfügbar – im Panel 'Developer Tools' manuell setzen."
    fi
else
    warn "Übersprungen (kein Admin) – im Panel 'Developer Tools' manuell setzen."
fi

# ═══════════════════════════════════════════════════════════════════
# Schritt 4: Alte Berechtigungs-Einträge entfernen
# ═══════════════════════════════════════════════════════════════════
step "Schritt 4: Alte Berechtigungs-Einträge entfernen"
echo ""
echo "  Ein bloßes Überschreiben der App genügt NICHT: In den Datenschutz-"
echo "  Listen können veraltete Einträge früherer Versionen (oder von anderen"
echo "  Pfaden wie Downloads) zurückbleiben und die neue Version blockieren."
echo "  Diese werden jetzt pro App zurückgesetzt – danach registriert sich"
echo "  die frisch installierte Version sauber neu."
echo ""
for dst in "${INSTALLED[@]}"; do
    bid="$(defaults read "$dst/Contents/Info.plist" CFBundleIdentifier 2>/dev/null)"
    [ -z "$bid" ] && bid="$(basename "$dst" .app)"
    # 'All' setzt sämtliche TCC-Dienste für diese Bundle-ID zurück und entfernt
    # damit auch Duplikate/Alt-Einträge mit gleicher Bundle-ID.
    tccutil reset All "$bid" >/dev/null 2>&1
    ok "$(basename "$dst")  ($bid)"
done

# ═══════════════════════════════════════════════════════════════════
# Schritt 5: Apps einmal starten → Selbst-Registrierung in den Listen
# ═══════════════════════════════════════════════════════════════════
step "Schritt 5: Apps registrieren"
echo ""
echo "  Die Apps werden einmal gestartet, damit sie automatisch in den"
echo "  Listen 'Bedienungshilfen' und 'Eingabeüberwachung' erscheinen."
echo ""
for dst in "${INSTALLED[@]}"; do
    open "$dst" 2>/dev/null && ok "gestartet: $(basename "$dst")"
done
echo ""
echo "  (Die Apps dürfen laufen bleiben – du kannst sie später über ihr"
echo "   Menüleisten-Symbol beenden.)"
sleep 3

# ═══════════════════════════════════════════════════════════════════
# Schritt 6: Datenschutz-Bereiche nacheinander öffnen
# ═══════════════════════════════════════════════════════════════════
step "Schritt 6: Berechtigungen erteilen (geführt)"
echo ""
echo "  Es werden nacheinander VIER Bereiche geöffnet. In jedem Bereich:"
echo ""
echo "    → Schalter für PunchBuddy, PunchBuddy_Watchdog und"
echo "      PunchBuddy_Diagnose auf EIN stellen."
echo "    → Falls eine App fehlt: '+'-Knopf → /Programme → App wählen."
echo "      (Bei Festplattenvollzugriff & Developer Tools ist '+' normal.)"
echo ""
pause "[Return → ersten Bereich öffnen] "

idx=1
total=${#PANES[@]}
for entry in "${PANES[@]}"; do
    label="${entry%%|*}"
    rest="${entry#*|}"
    anchor="${rest%%|*}"
    autoappear="${rest##*|}"

    echo ""
    echo "  [$idx/$total]  $label"
    if [ "$autoappear" = "ja" ]; then
        echo "         → Die drei Apps sollten bereits gelistet sein – Schalter auf EIN."
    else
        echo "         → '+'-Knopf → /Programme → die drei Apps hinzufügen, Schalter auf EIN."
    fi
    open "x-apple.systempreferences:com.apple.preference.security?$anchor" 2>/dev/null \
        || open "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension" 2>/dev/null

    if [ $idx -lt $total ]; then
        pause "[Erledigt? Return → nächster Bereich] "
    fi
    idx=$((idx+1))
done

# ═══════════════════════════════════════════════════════════════════
# Abschluss
# ═══════════════════════════════════════════════════════════════════
echo ""
echo "  ══════════════════════════════════════════════════════"
ok "Apps installiert (/Programme), Quarantäne entfernt, signiert"
$HAS_SUDO && ok "Developer-Mode aktiviert"
ok "Datenschutz-Bereiche geöffnet – Schalter gesetzt?"
echo ""
echo "  → Empfehlung: PunchBuddy einmal beenden und neu starten,"
echo "    damit alle Berechtigungen greifen."
echo ""
echo "  Drücke Return zum Beenden..."
read -r
