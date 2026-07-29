#!/usr/bin/env python3
"""Diagnose des Pro-Tools-„Export to OMF/AAF"-Dialogs.

Vom EIGENEN Terminal ausführen (dort sind die Bedienungshilfen/Accessibility
für AppleScript-UI-Automation gewährt):

    cd ~/PunchBuddy-Apple-Silicone
    python3 tools/aaf_dialog_diag.py

Selektiert die Export-Spuren (PTSL), öffnet den AAF-Export-Dialog, liest dessen
UI-Aufbau aus (Format-Popup + Optionen, Bit-Depth, Handle-Feld, Positionen) und
schließt ihn mit Escape wieder – ES WIRD NICHTS EXPORTIERT. Ergebnis landet in
/tmp/aaf_dialog_dump.txt.
"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from punchbuddy.engine import _get_engine  # noqa: E402
from punchbuddy.config import load_settings  # noqa: E402

OUT = "/tmp/aaf_dialog_dump.txt"

eng = _get_engine()
if eng is None:
    print("PTSL nicht verfügbar – läuft Pro Tools?")
    sys.exit(1)
tracks = load_settings().get("export_tracks", ["ST"])
try:
    eng.set_track_hidden_state(tracks, False)
except Exception as e:
    print("unhide:", e)
try:
    eng.select_tracks_by_name(tracks)
    print("Selektiert:", tracks)
except Exception as e:
    print("select:", e)

DIAG = r'''
tell application "Pro Tools" to activate
delay 0.6
tell application "System Events"
  tell process "Pro Tools"
    set frontmost to true
    delay 0.3
    try
      click menu item "Selected Tracks as New AAF/OMF..." of menu of menu item "Export" of menu "File" of menu bar 1
    on error
      try
        click menu item "Selected Tracks as New AAF/OMF…" of menu of menu item "Export" of menu "File" of menu bar 1
      on error errMsg
        return "ERROR:menu click failed: " & errMsg
      end try
    end try
    delay 2.0
    set tgt to missing value
    repeat with w in (every window)
      set nm to name of w
      if nm contains "Export to OMF" or nm contains "Export to AAF" or nm contains "OMF/AAF" then
        set tgt to w
        exit repeat
      end if
    end repeat
    if tgt is missing value then
      key code 53
      return "ERROR:export dialog not found"
    end if
    set outp to "WINDOW: " & (name of tgt) & linefeed
    set outp to outp & "== TOP-LEVEL POPUPS ==" & linefeed
    try
      repeat with p in (every pop up button of tgt)
        set outp to outp & "POPUP value=" & (value of p as text) & " pos=" & (position of p as text) & linefeed
        try
          set outp to outp & "   options=" & (name of every menu item of menu 1 of p as text) & linefeed
        end try
      end repeat
    end try
    set outp to outp & "== TOP-LEVEL TEXTFIELDS ==" & linefeed
    try
      repeat with tfx in (every text field of tgt)
        set outp to outp & "TEXT value=" & (value of tfx as text) & " pos=" & (position of tfx as text) & linefeed
      end repeat
    end try
    set gi to 0
    repeat with g in (every group of tgt)
      set gi to gi + 1
      set gt to ""
      try
        set gt to (title of g as text)
      end try
      set outp to outp & "== GROUP " & gi & " title='" & gt & "' pos=" & (position of g as text) & " ==" & linefeed
      try
        set pi to 0
        repeat with p in (every pop up button of g)
          set pi to pi + 1
          set outp to outp & "  POPUP#" & pi & " value=" & (value of p as text) & " pos=" & (position of p as text) & linefeed
          try
            set outp to outp & "     options=" & (name of every menu item of menu 1 of p as text) & linefeed
          end try
        end repeat
      end try
      try
        set ti to 0
        repeat with tfx in (every text field of g)
          set ti to ti + 1
          set outp to outp & "  TEXT#" & ti & " value=" & (value of tfx as text) & " pos=" & (position of tfx as text) & linefeed
        end repeat
      end try
      try
        repeat with cbx in (every checkbox of g)
          set outp to outp & "  CHECK '" & (title of cbx as text) & "' value=" & (value of cbx as text) & linefeed
        end repeat
      end try
    end repeat
    delay 0.3
    key code 53
    delay 0.3
    return outp
  end tell
end tell
'''

r = subprocess.run(["osascript", "-e", DIAG], capture_output=True, text=True, timeout=60)
result = r.stdout.strip() or r.stderr.strip()
with open(OUT, "w") as f:
    f.write(result + "\n")
print("\n=== ERGEBNIS (auch in %s) ===\n" % OUT)
print(result)
