#!/bin/bash
# PunchBuddy Diagnose-Tool
# Doppelklick im Finder startet die Diagnose.

# Terminal-Titel setzen
echo -e "\033]0;PunchBuddy Diagnose\007"
clear

echo "╔══════════════════════════════════════════════╗"
echo "║       PunchBuddy Diagnose-Tool               ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Python3 finden
PY=""
for candidate in python3 /usr/bin/python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    if command -v "$candidate" &>/dev/null; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "❌ Python3 nicht gefunden!"
    echo "   Bitte Python3 installieren: https://python.org"
    read -p "Enter drücken zum Schließen..."
    exit 1
fi

echo "✅ Python: $($PY --version 2>&1)"
echo ""
echo "Sammle Diagnosedaten..."
echo ""

# Python-Script inline ausführen
$PY - <<'PYEOF'
import os, sys, subprocess, glob, platform, datetime

OUT = os.path.expanduser("~/Desktop/PunchBuddy_Diagnostics.txt")
SEP = "\n" + "="*80 + "\n"

def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"[Fehler: {e}]"

def read_tail(path, n=200):
    if not os.path.exists(path):
        return f"[Datei nicht gefunden: {path}]"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-n:])
    except Exception as e:
        return f"[Lesefehler: {e}]"

def read_grep(path, patterns, n=400):
    if not os.path.exists(path):
        return f"[Datei nicht gefunden: {path}]"
    try:
        results = []
        pats = [p.lower() for p in patterns]
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if any(p in line.lower() for p in pats):
                    results.append(line)
        return "".join(results[-n:]) if results else "[Keine passenden Einträge]"
    except Exception as e:
        return f"[Lesefehler: {e}]"

def newest_file(pattern):
    files = glob.glob(os.path.expanduser(pattern))
    return max(files, key=os.path.getmtime) if files else None

out = []
out.append("=" * 80)
out.append(f"  PunchBuddy Diagnose-Report")
out.append(f"  Erstellt: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
out.append("=" * 80)

# 1. System
print("  [1/9] System-Info...")
out.append(SEP + "1. SYSTEM-INFO")
out.append(f"macOS:   {platform.mac_ver()[0]}")
out.append(f"Python:  {sys.version}")
out.append(f"Rechner: {platform.node()}")
out.append("\nInstallierte Pakete (relevant):")
out.append(run("pip3 list 2>/dev/null | grep -iE 'ptsl|rumps|pynput|pyobjc|grpc|protobuf'") or "[keine]")

# 2. PunchBuddy Log
print("  [2/9] PunchBuddy Log...")
out.append(SEP + "2. PUNCHBUDDY LOG (~/.punchbuddy/PunchBuddy.log) – letzte 300 Zeilen")
out.append(read_tail(os.path.expanduser("~/.punchbuddy/PunchBuddy.log"), 300))

# 3. Pro Tools Log (gefiltert)
print("  [3/9] Pro Tools Log (gefiltert)...")
out.append(SEP + "3. PRO TOOLS LOG – gefiltert (PTSL, Record, Input, Error, Timeout)")
pt_log = newest_file("~/Library/Logs/Avid/Pro_Tools_*.txt")
if pt_log:
    out.append(f"Datei: {pt_log}  ({os.path.getsize(pt_log):,} Bytes)")
    pats = ["ptsl","record","inputmon","input_mon","input mon","setrecord",
            "set_record","transport","error","lock","timeout","busy","grpc","0e28"]
    out.append(read_grep(pt_log, pats))
    out.append("\n--- Letzte 100 Zeilen (Kontext) ---")
    out.append(read_tail(pt_log, 100))
else:
    out.append("[Kein PT-Log gefunden – Pro Tools kurz schließen & neu starten um Log zu erzeugen]")

# 4. PT Log-Liste
print("  [4/9] PT Log-Übersicht...")
out.append(SEP + "4. VERFÜGBARE PRO TOOLS LOG-DATEIEN")
out.append(run("ls -lth ~/Library/Logs/Avid/Pro_Tools_*.txt 2>/dev/null | head -10"))

# 5. Console Logs
print("  [5/9] macOS Console Logs...")
out.append(SEP + "5. macOS CONSOLE LOGS – Pro Tools (letzte 30 Min)")
out.append(run(
    "log show --predicate 'process == \"Pro Tools\" OR eventMessage CONTAINS \"PTSL\" "
    "OR eventMessage CONTAINS \"InputMonitor\"' --last 30m --style syslog 2>/dev/null | tail -100",
    timeout=25
) or "[Keine Console-Logs / Berechtigung fehlt]")

# 6. Prozesse & Port
print("  [6/9] Prozess-Info...")
out.append(SEP + "6. PTSL / PRO TOOLS PROZESS-INFO")
out.append("Prozesse:\n" + run("ps aux | grep -iE 'pro tools|ptsl|ProTools' | grep -v grep"))
out.append("\nPort 31416 (PTSL):\n" + run("lsof -i :31416 2>/dev/null"))

# 7. Crash Reports
print("  [7/9] Crash Reports...")
out.append(SEP + "7. PRO TOOLS CRASH REPORTS (letzte 3)")
crashes = sorted(
    glob.glob(os.path.expanduser("~/Library/Logs/DiagnosticReports/*Pro*Tools*")) +
    glob.glob(os.path.expanduser("~/Library/Application Support/Avid/Pro Tools/Crashpad/**/*Pro*Tools*"), recursive=True),
    key=os.path.getmtime, reverse=True
)[:3]
if crashes:
    for c in crashes:
        out.append(f"\n--- {c} ---")
        out.append(read_tail(c, 60))
else:
    out.append("[Keine Crash-Reports gefunden]")

# 8. Settings
print("  [8/9] Einstellungen...")
out.append(SEP + "8. PUNCHBUDDY EINSTELLUNGEN (~/.punchbuddy/settings.json)")
out.append(read_tail(os.path.expanduser("~/.punchbuddy/settings.json")))

# 9. Disk
print("  [9/9] Disk-Info...")
out.append(SEP + "9. DISK INFO")
out.append(run("df -h ~"))
out.append(run("diskutil info / 2>/dev/null | grep -iE 'solid state|medium type|volume name|file system|device'"))

# Speichern
text = "\n".join(out)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(text)

print(f"\n  ✅ Fertig!")
print(f"  Datei: {OUT}")
print(f"  Größe: {os.path.getsize(OUT):,} Bytes")
PYEOF

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  ✅ Diagnose abgeschlossen!                  ║"
echo "║                                              ║"
echo "║  Datei: ~/Desktop/PunchBuddy_Diagnostics.txt ║"
echo "║                                              ║"
echo "║  Inhalt dieser Datei bitte kopieren und      ║"
echo "║  im Chat einfügen.                           ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Datei im Finder anzeigen
open -R ~/Desktop/PunchBuddy_Diagnostics.txt 2>/dev/null

read -p "Enter drücken zum Schließen..."
