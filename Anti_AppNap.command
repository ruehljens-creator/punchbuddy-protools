#!/bin/bash
#
# Anti_AppNap.command  –  nimmt die PunchBuddy-Steuerkette vom macOS App Nap aus
#
# HINTERGRUND (Befund 2026-07-21, Studio):
#   macOS drosselt Hintergrund-Apps ("App Nap"). Sobald Pro Tools im
#   Vordergrund ist, wird die Stream-Deck-App (inkl. des Chromium-Renderers,
#   in dem das Web-Requests-Plugin laeuft) gedrosselt – Tastendruecke kamen
#   dadurch erst SEKUNDEN spaeter bei PunchBuddy an. Mit den Ausnahmen
#   reagiert die Kette auch dann sofort, wenn Pro Tools vorne ist.
#
# BENUTZUNG: Doppelklick. Danach die betroffenen Apps EINMAL neu starten
#   (oder einfach den Rechner neu starten).
#
# RUECKGAENGIG: Doppelklick und beim Prompt "r" druecken – oder manuell:
#   defaults delete <BundleID> NSAppSleepDisabled
#
# Es wird NICHTS installiert und nichts sonst am System veraendert – nur die
# offizielle Apple-Einstellung NSAppSleepDisabled pro App gesetzt.

echo "==================================================="
echo "   Anti App Nap  (PunchBuddy-Steuerkette)"
echo "==================================================="
echo

MODE="setzen"
echo "Enter = Ausnahmen SETZEN   |   r + Enter = Ausnahmen ENTFERNEN"
read -r -p "> " _antwort
[ "$_antwort" = "r" ] || [ "$_antwort" = "R" ] && MODE="entfernen"
echo

# Kandidaten: App-Pfade der Steuerkette (nur vorhandene werden bearbeitet).
# Mehrere Pfade pro App erlaubt (Standard- und Studio-Installationsorte).
CANDIDATES=(
  "/Applications/Elgato Stream Deck.app"
  "/Applications/PunchBuddy.app"
  "/Applications/Script_Selbstmische/PunchBuddy.app"
  "/Applications/PunchBuddy_Watchdog.app"
  "/Applications/Script_Selbstmische/PunchBuddy_Watchdog.app"
  "/Applications/Pro Tools.app"
  "/Applications/Interplay Access.app"
  "/Applications/Avid/Interplay Access.app"
  "/Applications/MediaCentral Cloud UX Desktop.app"
)

FOUND=0
for APP in "${CANDIDATES[@]}"; do
  [ -d "$APP" ] || continue
  BID="$(defaults read "$APP/Contents/Info" CFBundleIdentifier 2>/dev/null)"
  [ -n "$BID" ] || continue
  FOUND=$((FOUND+1))
  NAME="$(basename "$APP" .app)"
  if [ "$MODE" = "setzen" ]; then
    defaults write "$BID" NSAppSleepDisabled -bool YES
    printf "  [OK] %-28s (%s)  -> App Nap AUS\n" "$NAME" "$BID"
  else
    defaults delete "$BID" NSAppSleepDisabled 2>/dev/null
    printf "  [OK] %-28s (%s)  -> Ausnahme entfernt\n" "$NAME" "$BID"
  fi
done

echo
if [ "$FOUND" -eq 0 ]; then
  echo "  Keine der bekannten Apps gefunden – nichts geaendert."
else
  echo "==================================================="
  echo "  FERTIG: $FOUND App(s) bearbeitet (Modus: $MODE)."
  echo "  WICHTIG: Betroffene Apps einmal neu starten,"
  echo "  damit die Einstellung wirkt."
  echo
  echo "  Kontrolle: Aktivitaetsanzeige -> Ansicht ->"
  echo "  Spalte 'App Nap' einblenden -> muss 'Nein' zeigen."
  echo "==================================================="
fi
echo
read -n1 -r -p "Zum Schliessen eine beliebige Taste druecken..."
echo
