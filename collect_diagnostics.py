#!/usr/bin/env python3
"""
PunchBuddy Diagnose-Script
============================
Führe dieses Script auf dem Studio-Rechner aus:
    python3 collect_diagnostics.py

Das Script sammelt alle relevanten Logs und speichert sie in:
    ~/Desktop/PunchBuddy_Diagnostics.txt

Den Inhalt dieser Datei dann hier einfügen.
"""

import os, sys, subprocess, glob, platform, datetime, shutil

OUT = os.path.expanduser("~/Desktop/PunchBuddy_Diagnostics.txt")
SEP = "\n" + "="*80 + "\n"

def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except Exception as e:
        return f"[Fehler: {e}]"

def read_tail(path, lines=200):
    """Liest die letzten N Zeilen einer Datei."""
    if not os.path.exists(path):
        return f"[Datei nicht gefunden: {path}]"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        return "".join(all_lines[-lines:])
    except Exception as e:
        return f"[Lesefehler: {e}]"

def read_head(path, lines=120):
    """Liest die ersten N Zeilen einer Datei (Crash-Reports: Exception-Info
    und crashender Thread stehen am Anfang)."""
    if not os.path.exists(path):
        return f"[Datei nicht gefunden: {path}]"
    try:
        result = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= lines:
                    break
                result.append(line)
        return "".join(result)
    except Exception as e:
        return f"[Lesefehler: {e}]"

def read_grep(path, patterns, lines=300):
    """Liest alle Zeilen einer Datei die einem der Patterns entsprechen."""
    if not os.path.exists(path):
        return f"[Datei nicht gefunden: {path}]"
    try:
        results = []
        pats = [p.lower() for p in patterns]
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if any(p in line.lower() for p in pats):
                    results.append(line)
        if not results:
            return "[Keine passenden Einträge gefunden]"
        return "".join(results[-lines:])
    except Exception as e:
        return f"[Lesefehler: {e}]"

def newest_file(pattern):
    """Findet die neueste Datei die dem Glob-Pattern entspricht."""
    files = glob.glob(os.path.expanduser(pattern))
    if not files:
        return None
    return max(files, key=os.path.getmtime)

lines = []
lines.append("=" * 80)
lines.append(f"  PunchBuddy Diagnose-Report")
lines.append(f"  Erstellt: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
lines.append("=" * 80)

# ── 1. System-Info ─────────────────────────────────────────────────────────
lines.append(SEP + "1. SYSTEM-INFO")
lines.append(f"macOS:   {platform.mac_ver()[0]}")
lines.append(f"Python:  {sys.version}")
lines.append(f"Rechner: {platform.node()}")
try:
    from punchbuddy.version import __version__ as _pbv
    lines.append(f"PunchBuddy-Version (Diagnose-Build): v{_pbv}")
except Exception:
    lines.append("PunchBuddy-Version: [nicht ermittelbar]")
lines.append(f"\nInstallierte Python-Pakete (relevant):")
pkgs = run("pip3 list 2>/dev/null | grep -i 'ptsl\\|rumps\\|pynput\\|pyobjc\\|grpc\\|protobuf'")
lines.append(pkgs or "[keine gefunden]")

# ── 2. PunchBuddy Log (komplett, neueste 300 Zeilen) ──────────────────────
lines.append(SEP + "2. PUNCHBUDDY LOG  (~/.punchbuddy/PunchBuddy.log) – letzte 300 Zeilen")
log_path = os.path.expanduser("~/.punchbuddy/PunchBuddy.log")
lines.append(read_tail(log_path, 300))

# ── 3. Pro Tools Log (neueste Datei, gefiltert) ────────────────────────────
lines.append(SEP + "3. PRO TOOLS LOG (~/Library/Logs/Avid/Pro_Tools_*.txt) – gefiltert")
pt_log = newest_file("~/Library/Logs/Avid/Pro_Tools_*.txt")
if pt_log:
    lines.append(f"Datei: {pt_log}")
    lines.append(f"Größe: {os.path.getsize(pt_log):,} Bytes")
    lines.append("\n--- Relevante Einträge (PTSL, Record, Input, Error, Lock, Timeout) ---")
    patterns = ["ptsl", "record", "inputmon", "input_mon", "input mon",
                "setrecord", "set_record", "transport", "error", "lock",
                "timeout", "busy", "waiting", "grpc", "0e28"]
    lines.append(read_grep(pt_log, patterns, lines=400))
    lines.append("\n--- Letzte 100 Zeilen des PT Logs (Kontext) ---")
    lines.append(read_tail(pt_log, 100))
else:
    lines.append("[Keine Pro Tools Log-Datei gefunden – Pro Tools muss nach Session beendet werden]")

# ── 3b. Interplay / MediaCentral Export-Analyse ───────────────────────────
lines.append(SEP + "3b. INTERPLAY / MEDIACENTRAL EXPORT – PT Log-Analyse")
if pt_log:
    lines.append("--- Interplay/Export-Einträge im PT Log ---")
    export_patterns = [
        "interplay", "mediacentral", "media central", "nexis", "mpm",
        "media production", "export", "progress:", "dfw:", "send",
        "transfer", "avid link", "avidlink", "publish", "complete",
        "success", "fail", "error", "dialog", "alert", "notification"
    ]
    lines.append(read_grep(pt_log, export_patterns, lines=500))
else:
    lines.append("[Kein PT Log gefunden]")

lines.append("\n--- Avid Link / AvidControlDesktop Logs ---")
avid_link_log = newest_file("~/Library/Logs/Avid/AvidControlDesktop/*.txt")
if avid_link_log:
    lines.append(f"Datei: {avid_link_log}")
    lines.append(read_tail(avid_link_log, 100))
else:
    lines.append("[Kein AvidControlDesktop Log gefunden]")

lines.append("\n--- macOS Unified Log: Avid/Interplay Prozesse (letzte 30 Min) ---")
unified_avid = run(
    "log show --predicate '"
    "process CONTAINS \"Avid\" OR process CONTAINS \"Interplay\" "
    "OR process CONTAINS \"MediaCentral\" OR process CONTAINS \"AvidLink\" "
    "OR eventMessage CONTAINS \"interplay\" OR eventMessage CONTAINS \"mediacentral\"' "
    "--last 30m --style compact 2>/dev/null | tail -150",
    timeout=30
)
lines.append(unified_avid or "[Keine Einträge oder Berechtigung fehlt]")

# ── 4. Alle Pro Tools Logs auflisten ──────────────────────────────────────
lines.append(SEP + "4. VERFÜGBARE PRO TOOLS LOG-DATEIEN")
lines.append(run("ls -lth ~/Library/Logs/Avid/Pro_Tools_*.txt 2>/dev/null | head -10"))

# ── 5. macOS Console Logs (Pro Tools / PTSL) ──────────────────────────────
lines.append(SEP + "5. macOS CONSOLE LOGS – Pro Tools (letzte 30 Min)")
# log show braucht evtl. sudo, probieren wir es trotzdem
console_out = run(
    "log show --predicate 'process == \"Pro Tools\" OR subsystem CONTAINS \"ptsl\" "
    "OR eventMessage CONTAINS \"PTSL\" OR eventMessage CONTAINS \"InputMonitor\"' "
    "--last 30m --style syslog 2>/dev/null | tail -100",
    timeout=20
)
lines.append(console_out or "[Keine Console-Logs verfügbar oder Berechtigung fehlt]")

# ── 6. PTSL Server Prozess ────────────────────────────────────────────────
lines.append(SEP + "6. PTSL / PRO TOOLS PROZESS-INFO")
lines.append("Laufende Prozesse:")
lines.append(run("ps aux | grep -i 'pro tools\\|ptsl\\|ProTools' | grep -v grep"))
lines.append("\nOffene Netzwerk-Ports (PTSL = 31416):")
lines.append(run("lsof -i :31416 2>/dev/null || netstat -an 2>/dev/null | grep 31416"))

# ── 7. Crash Reports ──────────────────────────────────────────────────────
lines.append(SEP + "7. PRO TOOLS CRASH REPORTS (letzte 3)")
crash_dirs = [
    "~/Library/Logs/DiagnosticReports/",
    "~/Library/Application Support/Avid/Pro Tools/Crashpad/",
]
found_crashes = []
for d in crash_dirs:
    pattern = os.path.expanduser(d) + "/**/*Pro*Tools*"
    found = glob.glob(pattern, recursive=True)
    found_crashes.extend(found)
found_crashes = sorted(found_crashes, key=os.path.getmtime, reverse=True)[:3]
if found_crashes:
    for cf in found_crashes:
        lines.append(f"\n--- {cf} ---")
        lines.append(read_tail(cf, 50))
else:
    lines.append("[Keine Pro Tools Crash-Reports gefunden]")

# ── 7b. PunchBuddy Crash Reports ──────────────────────────────────────────
lines.append(SEP + "7b. PUNCHBUDDY CRASH REPORTS (letzte 3)")
pb_crash_patterns = [
    "~/Library/Logs/DiagnosticReports/PunchBuddy*",
    "~/Library/Logs/DiagnosticReports/Retired/PunchBuddy*",
    "~/Library/Logs/DiagnosticReports/Python*",
    "~/Library/Logs/DiagnosticReports/Retired/Python*",
]
pb_crashes = []
for pat in pb_crash_patterns:
    pb_crashes.extend(glob.glob(os.path.expanduser(pat)))
pb_crashes = sorted(pb_crashes, key=os.path.getmtime, reverse=True)[:3]
if pb_crashes:
    for cf in pb_crashes:
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(cf))
        lines.append(f"\n--- {cf} (geändert: {mtime:%Y-%m-%d %H:%M:%S}) ---")
        lines.append(read_head(cf, 120))
else:
    lines.append("[Keine PunchBuddy Crash-Reports gefunden]")

lines.append("\n--- PunchBuddy faulthandler-Log (native Abstürze) ---")
fault_log_candidates = [
    "~/.punchbuddy/PunchBuddy_crash.log",
    "~/Library/Logs/PunchBuddy/PunchBuddy_crash.log",
    "/tmp/PunchBuddy/PunchBuddy_crash.log",
]
for cand in fault_log_candidates:
    cand = os.path.expanduser(cand)
    if os.path.exists(cand):
        lines.append(f"Datei: {cand}")
        lines.append(read_tail(cand, 100))
        break
else:
    lines.append("[Kein faulthandler-Log gefunden]")

# ── 8. Settings ───────────────────────────────────────────────────────────
lines.append(SEP + "8. PUNCHBUDDY EINSTELLUNGEN")
settings_path = os.path.expanduser("~/.punchbuddy/settings.json")
lines.append(read_tail(settings_path, 50))

# ── 9. Disk-Info (Schreibgeschwindigkeit relevant für PT-Cleanup-Zeit) ─────
lines.append(SEP + "9. DISK INFO")
lines.append(run("df -h ~ 2>/dev/null"))
lines.append(run("diskutil info / 2>/dev/null | grep -i 'solid state\\|medium type\\|volume name\\|file system'"))

# ── 10. NETZWERK (Interfaces, Service-Order, Routing, DNS) ─────────────────
# Wichtig: Pro Tools Video-Engine/Satellite-Link bindet ggf. an die falsche
# (geroutete Firmen-/NEXIS-)NIC statt Loopback → Transport-Stalls.
lines.append(SEP + "10. NETZWERK – Interfaces / Service-Order / Routing / DNS")
lines.append("--- Aktive Interfaces (ifconfig, nur inet) ---")
lines.append(run("ifconfig | grep -E '^[a-z]|inet ' | grep -v inet6"))
lines.append("\n--- Netzwerk-Service-Reihenfolge (Set Service Order – oberste = primäre Route) ---")
lines.append(run("networksetup -listnetworkserviceorder 2>/dev/null"))
lines.append("\n--- Hardware-Ports ---")
lines.append(run("networksetup -listallhardwareports 2>/dev/null"))
lines.append("\n--- Default-Route / primäres Interface ---")
lines.append(run("route -n get default 2>/dev/null | grep -E 'interface|gateway'"))
lines.append("\n--- Routing-Tabelle (Auszug) ---")
lines.append(run("netstat -rn 2>/dev/null | head -25"))
lines.append("\n--- DNS-Konfiguration ---")
lines.append(run("scutil --dns 2>/dev/null | grep -E 'nameserver|domain' | head -15"))

# ── 11. PT SATELLITE / VIDEO-ENGINE NETZWERK-PORTS ─────────────────────────
lines.append(SEP + "11. PT SATELLITE / VIDEO-ENGINE / PTSL NETZWERK-PORTS")
lines.append("PTSL=31416, PunchBuddy-HTTP=8899, Satellite/Video-Engine=28282/28284")
lines.append("--- lsof auf den relevanten Ports (IP vor Port: 127.0.0.1=lokal/gut, 10.x/LAN=geroutet) ---")
lines.append(run("lsof -nP -iTCP:31416 -iTCP:8899 -iTCP:28282 -iTCP:28284 -iUDP:28282 -iUDP:28284 2>/dev/null"))
lines.append("\n--- netstat (31416/8899/2828x), CLOSE_WAIT/ESTABLISHED prüfen ---")
lines.append(run("netstat -an 2>/dev/null | grep -E '31416|8899|2828' "))
lines.append("\n--- AvidVideoEngine-Prozess (servicehint vidsat = Video-Satellite) ---")
lines.append(run("ps aux | grep -i 'AvidVideoEngine\\|vidsat' | grep -v grep"))
lines.append("\n--- Satellite/Clock-Sync-Marker im PT-Log (sollten bei aktiver Video-Engine erscheinen) ---")
if pt_log:
    lines.append(read_grep(pt_log, ["SLnk_", "CSync", "vidsat", "UME_LockToNetworkClock",
                                    "SyncRemoteClocks", "eSynchronizerState", "WaitingTrigger",
                                    "LockToSatellite", "ControlLock", "network clock"], lines=120))
else:
    lines.append("[Kein PT-Log]")

# ── 12. MICROSOFT DEFENDER (Status + Netzwerk-Extension) ───────────────────
# 'Deaktiviert' im UI heißt NICHT, dass die Netzwerk-Extension entladen ist.
# WICHTIG: mdatp liegt in /usr/local/bin – das ist in der App-Umgebung NICHT im
# PATH (deshalb waren diese Felder in früheren Reports leer) → absoluter Pfad.
lines.append(SEP + "12. MICROSOFT DEFENDER FOR ENDPOINT – Status & Netzwerk-Extension")
MDATP = "/usr/local/bin/mdatp"
lines.append("--- mdatp health (Kernfelder) ---")
lines.append(run(f"M={MDATP}; [ -x $M ] || M=mdatp; "
                 "for f in healthy real_time_protection_enabled passive_mode_enabled "
                 "network_protection_status behavior_monitoring tamper_protection managed_by; do "
                 "printf '%s: ' $f; $M health --field $f 2>/dev/null || echo '(n/a)'; done"))
lines.append("\n--- mdatp Versionen (Produkt/Engine/Definitionen) – für Update-Vergleiche ---")
lines.append(run(f"M={MDATP}; [ -x $M ] || M=mdatp; "
                 "$M version 2>/dev/null; "
                 "for f in app_version engine_version definitions_version definitions_status "
                 "definitions_updated definitions_updated_minutes_ago product_expiration; do "
                 "printf '%s: ' $f; $M health --field $f 2>/dev/null || echo '(n/a)'; done"))
lines.append("\n--- Extension-Bundle-Versionen (ändern sich bei Defender-Updates) ---")
lines.append(run("for d in /Library/SystemExtensions/*/com.microsoft.wdav.*.systemextension; do "
                 "echo \"$d\"; plutil -p \"$d/Contents/Info.plist\" 2>/dev/null "
                 "| grep -E 'CFBundleShortVersionString|CFBundleVersion'; done"))
lines.append("\n--- Defender Update-/Install-Historie (Log-Auszug mit Zeitstempeln) ---")
lines.append(run("ls -lat /Library/Logs/Microsoft/mdatp/ 2>/dev/null | head -8; "
                 "grep -ihE 'update|definition|upgraded|installed|version' "
                 "/Library/Logs/Microsoft/mdatp/install.log "
                 "/Library/Logs/Microsoft/mdatp/*core*.log 2>/dev/null | tail -25"))
lines.append("\n--- mdatp System-Extensions (network_extension_enabled/-installed) ---")
lines.append(run(f"M={MDATP}; [ -x $M ] || M=mdatp; $M health --details system_extensions 2>/dev/null"))
lines.append("\n--- wdav/mdatp/Defender-Prozesse (CPU!) ---")
lines.append(run("ps aux | grep -iE 'wdav|mdatp|defender' | grep -v grep"))
lines.append("\n--- Defender-Diagnose-/Log-Ablage vorhanden? ---")
lines.append(run("ls -la '/Library/Application Support/Microsoft/Defender/wdavdiag/' 2>/dev/null | tail -5"))

# ── 13. SYSTEM-EXTENSIONS / NETZWERK-FILTER (systemweit) ───────────────────
lines.append(SEP + "13. SYSTEM-EXTENSIONS / NETZWERK-FILTER")
lines.append("--- systemextensionsctl list (achten auf com.microsoft.wdav.netext / Cisco / sonstige Filter) ---")
lines.append(run("systemextensionsctl list 2>/dev/null"))

# ── 14. macOS FIREWALL ─────────────────────────────────────────────────────
lines.append(SEP + "14. macOS FIREWALL")
lines.append("--- Application Firewall: Global State (0=aus,1=an,2=block all) ---")
lines.append(run("/usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate 2>/dev/null; "
                 "/usr/libexec/ApplicationFirewall/socketfilterfw --getblockall 2>/dev/null; "
                 "/usr/libexec/ApplicationFirewall/socketfilterfw --getstealthmode 2>/dev/null"))
lines.append("\n--- ALF-Prefs (globalstate) ---")
lines.append(run("defaults read /Library/Preferences/com.apple.alf globalstate 2>/dev/null"))
lines.append("\n--- pf-Firewall Status (braucht ggf. sudo) ---")
lines.append(run("pfctl -s info 2>&1 | head -8"))
lines.append("\n--- Firewall-Regeln für AvidVideoEngine/Pro Tools? ---")
lines.append(run("/usr/libexec/ApplicationFirewall/socketfilterfw --listapps 2>/dev/null | grep -iE 'Pro Tools|AvidVideoEngine|Video Engine|Python|PunchBuddy' -A1"))

# ── 15. STREAM DECK (Elgato) – Prozess, Version, Logs ──────────────────────
lines.append(SEP + "15. STREAM DECK (Elgato) – Prozess / Version / Logs")
lines.append("--- Laufender Prozess ---")
lines.append(run("ps aux | grep -i 'Stream Deck\\|StreamDeck\\|elgato' | grep -v grep"))
lines.append("\n--- App-Version ---")
lines.append(run("defaults read '/Applications/Elgato Stream Deck.app/Contents/Info.plist' CFBundleShortVersionString 2>/dev/null || echo '[App nicht gefunden]'"))
lines.append("\n--- Stream-Deck-Logs (neueste Zeilen) ---")
_sd_logdirs = [
    "~/Library/Logs/ElgatoStreamDeck",
    "~/Library/Application Support/com.elgato.StreamDeck/logs",
    "~/Library/Application Support/Elgato/StreamDeck/logs",
]
_sd_found = False
for _d in _sd_logdirs:
    _newest = newest_file(_d + "/*.log")
    if _newest:
        lines.append(f"Datei: {_newest}")
        lines.append(read_tail(_newest, 60))
        _sd_found = True
        break
if not _sd_found:
    lines.append("[Keine Stream-Deck-Logs gefunden – Pfade geprüft: " + ", ".join(_sd_logdirs) + "]")
lines.append("\n--- Autostart/Login-Items (Stream Deck) ---")
lines.append(run("osascript -e 'tell application \"System Events\" to get the name of every login item' 2>/dev/null"))

# ── 16. VOLLSTÄNDIGE PROZESSLISTE (Top-CPU) + Auffälligkeiten ──────────────
lines.append(SEP + "16. PROZESSE – Top-CPU + relevante Dritt-Software")
lines.append("--- Top 20 nach CPU ---")
lines.append(run("ps aux | sort -nrk 3 | head -20"))
lines.append("\n--- Sicherheits-/Netzwerk-Dritt-Software (Defender/Cisco/Umbrella/VPN/Filter) ---")
lines.append(run("ps aux | grep -iE 'wdav|mdatp|defender|cisco|umbrella|anyconnect|crowdstrike|sentinel|netskope|zscaler|little snitch|lulu' | grep -v grep | head -20"))

# ── 17. LATENZ-SELBSTTEST (Haken-/Verzögerungs-Diagnose, 2026-07-21) ───────
# Misst die drei Etappen des Stream-Deck-Wegs getrennt:
#   Taste → HTTP-Antwort (17a, Weg des Web-Requests-Plugins/Haken),
#   Taste → Socket-Antwort (17b, Weg der .app-Launcher),
#   PunchBuddy → Pro Tools (17c, PTSL – bekannt bimodal 10ms/300-1400ms).
# Dazu Proxy-Konfig (17d), Auswertung der neuen ms-Logzeilen (17e) und
# Web-Requests-Plugin-Status (17f).
lines.append(SEP + "17. LATENZ-SELBSTTEST – Webtrigger / Socket / PTSL / Proxy")

import time as _time
import socket as _socket
import urllib.request as _urlreq

# 17a) HTTP-Loopback – exakt der Weg des Web-Requests-Plugins (grüner Haken)
lines.append("--- 17a. HTTP-Webtrigger http://127.0.0.1:8899/command/ping (10x, ms) ---")
_http_times = []
for _i in range(10):
    _t0 = _time.time()
    try:
        with _urlreq.urlopen("http://127.0.0.1:8899/command/ping", timeout=5) as _r:
            _body = _r.read(64).decode("utf-8", "replace").strip()
        _dt = (_time.time() - _t0) * 1000
        _http_times.append(_dt)
        lines.append(f"  {_i+1:2d}: {_dt:7.1f} ms  -> {_body}")
    except Exception as _e:
        lines.append(f"  {_i+1:2d}: FEHLER: {_e}")
        break
    _time.sleep(0.2)
if _http_times:
    _s = sorted(_http_times)
    lines.append(f"  => min={_s[0]:.1f}  median={_s[len(_s)//2]:.1f}  max={_s[-1]:.1f} ms")
    lines.append("  Bewertung: einstellige ms = Server ok. Braucht der HAKEN am Stream Deck")
    lines.append("  trotzdem Sekunden, sitzt die Bremse in der Stream-Deck-App (Chromium/Plugin).")
else:
    lines.append("  [PunchBuddy-HTTP nicht erreichbar – läuft PunchBuddy gerade?]")

# 17b) Unix-Socket – der Weg der .app-Launcher
lines.append("\n--- 17b. Unix-Socket /tmp/punchbuddy.sock (5x ping, ms) ---")
_sock_ok = False
for _i in range(5):
    _t0 = _time.time()
    try:
        _c = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        _c.settimeout(5.0)
        _c.connect("/tmp/punchbuddy.sock")
        _c.sendall(b"ping\n")
        _resp = _c.recv(128).decode("utf-8", "replace").strip()
        _c.close()
        _dt = (_time.time() - _t0) * 1000
        lines.append(f"  {_i+1:2d}: {_dt:7.1f} ms  -> {_resp}")
        _sock_ok = True
    except Exception as _e:
        lines.append(f"  {_i+1:2d}: FEHLER: {_e}")
        break
    _time.sleep(0.2)
if not _sock_ok:
    lines.append("  [Socket nicht erreichbar – PunchBuddy aus oder Socket deaktiviert]")

# 17c) PTSL-Direktmessung (nur wenn Pro Tools läuft) – bimodale Latenz messen
lines.append("\n--- 17c. PTSL-Latenz direkt (transport_state, 10x, ms) ---")
_pt_reachable = False
try:
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _probe.settimeout(0.3)
    _pt_reachable = (_probe.connect_ex(("127.0.0.1", 31416)) == 0)
    _probe.close()
except Exception:
    pass
if not _pt_reachable:
    lines.append("  [PTSL-Port 31416 nicht offen – Pro Tools läuft nicht / PTSL aus]")
else:
    try:
        import ptsl as _ptsl
        _t0 = _time.time()
        _eng = _ptsl.engine.Engine(company_name="PunchBuddy", application_name="Diagnose")
        lines.append(f"  Verbindungsaufbau: {(_time.time()-_t0)*1000:.0f} ms")
        _pts = []
        for _i in range(10):
            _t0 = _time.time()
            try:
                _st = str(_eng.transport_state())
            except Exception as _e:
                _st = f"ERR {_e}"
            _dt = (_time.time() - _t0) * 1000
            _pts.append(_dt)
            lines.append(f"  {_i+1:2d}: {_dt:7.1f} ms  -> {_st}")
            if _dt > 5000:
                lines.append("  [Abbruch: Einzel-Call > 5s – PT hängt vermutlich]")
                break
        try:
            _eng.close()
        except Exception:
            pass
        if _pts:
            _fast = sum(1 for x in _pts if x < 50)
            _slow = sum(1 for x in _pts if x > 300)
            _s = sorted(_pts)
            lines.append(f"  => min={_s[0]:.0f}  median={_s[len(_s)//2]:.0f}  max={_s[-1]:.0f} ms"
                         f"  | schnell(<50ms): {_fast}  langsam(>300ms): {_slow}")
            lines.append("  Bewertung: bimodal (~50/50 schnell/langsam) = bekanntes PT-Verhalten.")
            lines.append("  Median deutlich >500ms / viele >1s = PT-Zustand schlecht (Uptime? Satellite?).")
    except Exception as _e:
        lines.append(f"  [PTSL-Messung nicht möglich: {_e}]")

# 17d) Proxy-Konfiguration (Chromium/fetch des SD-Plugins respektiert System-Proxy)
lines.append("\n--- 17d. System-Proxy (scutil --proxy) ---")
lines.append(run("scutil --proxy"))

# 17e) Auswertung der neuen ms-Loginstrumentierung
lines.append("\n--- 17e. PunchBuddy-Log: langsame PTSL-Calls / Schutzfenster / Lock-Stau ---")
_pb_log = os.path.expanduser("~/.punchbuddy/PunchBuddy.log")
lines.append("Zähler:")
lines.append(run(f"grep -c 'PTSL langsam' '{_pb_log}' 2>/dev/null | xargs echo '  PTSL langsam:'"))
lines.append(run(f"grep -c 'Schutzfenster' '{_pb_log}' 2>/dev/null | xargs echo '  Schutzfenster-Verwuerfe:'"))
lines.append(run(f"grep -c 'Lock nicht erhalten' '{_pb_log}' 2>/dev/null | xargs echo '  Lock nicht erhalten:'"))
lines.append("Letzte relevante Zeilen (mit ms-Zeitstempeln):")
lines.append(read_grep(_pb_log, ["PTSL langsam", "Schutzfenster", "Lock nicht erhalten",
                                 ">>> TRIGGER"], lines=40))

# 17f) Web-Requests-Plugin (Haken-Plugin) installiert?
lines.append("\n--- 17f. Stream-Deck-Plugins (installiert) ---")
lines.append(run("ls '" + os.path.expanduser(
    "~/Library/Application Support/com.elgato.StreamDeck/Plugins") + "' 2>/dev/null"))
_wr_manifest = os.path.expanduser(
    "~/Library/Application Support/com.elgato.StreamDeck/Plugins/"
    "gg.datagram.web-requests.sdPlugin/manifest.json")
lines.append("Web-Requests-Plugin: " + ("installiert" if os.path.exists(_wr_manifest)
                                        else "NICHT installiert"))
lines.append("Hinweis Haken-Test: Taste drücken + Uhrzeit notieren, dann oben in 17e den")
lines.append("'>>> TRIGGER'-Zeitstempel vergleichen. Lücke = Zustellweg (SD-App); keine")
lines.append("Lücke, aber Haken spät = Antwortweg zum Plugin.")

# ── Speichern ─────────────────────────────────────────────────────────────
output = "\n".join(lines)
with open(OUT, "w", encoding="utf-8") as f:
    f.write(output)

print(f"\n✅ Diagnose gespeichert: {OUT}")
print(f"   Größe: {os.path.getsize(OUT):,} Bytes")
print(f"\nBitte den Inhalt von '{OUT}' hier einfügen.\n")

# Optional: direkt im Finder öffnen
try:
    subprocess.run(["open", "-R", OUT])
except:
    pass
