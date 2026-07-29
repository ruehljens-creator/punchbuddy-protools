-- Anti_AppNap.app – nimmt die PunchBuddy-Steuerkette vom macOS App Nap aus
--
-- HINTERGRUND (Befund 2026-07-21, Studio): macOS drosselt Hintergrund-Apps
-- ("App Nap"). Sobald Pro Tools im Vordergrund ist, wurde die Stream-Deck-App
-- (inkl. des Chromium-Renderers des Web-Requests-Plugins) gedrosselt –
-- Tastendruecke kamen erst Sekunden spaeter bei PunchBuddy an.
--
-- Die App setzt/entfernt NUR die offizielle Apple-Einstellung
-- NSAppSleepDisabled fuer die gefundenen Apps. Nichts wird installiert.
-- Build: osacompile -o Anti_AppNap.app Anti_AppNap.applescript

on run
	set introText to "Anti App Nap – PunchBuddy-Steuerkette" & return & return & ¬
		"macOS drosselt Hintergrund-Apps (App Nap). Dadurch reagierten Stream-Deck-Befehle träge, sobald Pro Tools im Vordergrund war." & return & return & ¬
		"‚Setzen' nimmt Stream Deck, PunchBuddy (+Watchdog), Pro Tools und Interplay/MediaCentral davon aus. ‚Entfernen' macht alles rückgängig."
	set userChoice to button returned of (display dialog introText ¬
		buttons {"Abbrechen", "Entfernen", "Setzen"} default button "Setzen" ¬
		with title "Anti App Nap")
	if userChoice is "Abbrechen" then return

	set shellMode to "set"
	if userChoice is "Entfernen" then set shellMode to "remove"

	set shellScript to "MODE=" & shellMode & "
CANDIDATES='/Applications/Elgato Stream Deck.app
/Applications/PunchBuddy.app
/Applications/Script_Selbstmische/PunchBuddy.app
/Applications/PunchBuddy_Watchdog.app
/Applications/Script_Selbstmische/PunchBuddy_Watchdog.app
/Applications/Pro Tools.app
/Applications/Interplay Access.app
/Applications/Avid/Interplay Access.app
/Applications/MediaCentral Cloud UX Desktop.app'
FOUND=0
REPORT=''
while IFS= read -r APP; do
  [ -d \"$APP\" ] || continue
  BID=$(defaults read \"$APP/Contents/Info\" CFBundleIdentifier 2>/dev/null)
  [ -n \"$BID\" ] || continue
  FOUND=$((FOUND+1))
  NAME=$(basename \"$APP\" .app)
  if [ \"$MODE\" = set ]; then
    defaults write \"$BID\" NSAppSleepDisabled -bool YES
    REPORT=\"$REPORT✓  $NAME – App Nap AUS
\"
  else
    defaults delete \"$BID\" NSAppSleepDisabled 2>/dev/null
    REPORT=\"$REPORT✓  $NAME – Ausnahme entfernt
\"
  fi
done <<EOF2
$CANDIDATES
EOF2
if [ \"$FOUND\" -eq 0 ]; then
  echo 'Keine der bekannten Apps gefunden – nichts geändert.'
else
  printf '%s\\n%s Apps bearbeitet.\\n\\nWICHTIG: Betroffene Apps einmal neu starten, damit die Einstellung wirkt.\\n\\nKontrolle: Aktivitätsanzeige → Ansicht → Spalte „App Nap\" → muss „Nein\" zeigen.' \"$REPORT\" \"$FOUND\"
fi"

	set reportText to do shell script shellScript
	display dialog reportText buttons {"OK"} default button "OK" with title "Anti App Nap – Ergebnis"
end run
