"""Log-Verzeichnis, -Pfade und Logging-Setup.

Blatt-Modul (nur stdlib). Pfad-Konstanten werden beim Import berechnet, das
Logging wird beim Import konfiguriert – wie zuvor im Monolithen.
"""
import os
import sys
import logging
import threading
import faulthandler

def _setup_log_dir():
    """Erstellt das Log-Verzeichnis, robust gegen TCC und alte Dateien."""
    candidates = [
        os.path.expanduser("~/.punchbuddy"),
        os.path.expanduser("~/Library/Logs/PunchBuddy"),
        "/tmp/PunchBuddy",
    ]
    for d in candidates:
        try:
            # os.path.exists() kann unter TCC selbst EPERM werfen
            try:
                exists = os.path.exists(d)
                is_dir = os.path.isdir(d) if exists else False
            except OSError:
                continue  # TCC blockiert Zugriff – nächster Kandidat

            if exists and not is_dir:
                continue  # Existiert als Datei/Symlink – überspringen

            os.makedirs(d, exist_ok=True)
            # Schreibtest
            test = os.path.join(d, ".writetest")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            return d
        except (OSError, PermissionError):
            continue
    return "/tmp"

_LOG_DIR = _setup_log_dir()
LOG_PATH = os.path.join(_LOG_DIR, "PunchBuddy.log")

def _trim_log(max_age_hours=24):
    """Entfernt Log-Einträge die älter als max_age_hours sind."""
    if not os.path.exists(LOG_PATH):
        return
    try:
        from datetime import datetime, timedelta
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        today = datetime.now().date()
        kept = []
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Log-Format: "HH:MM:SS ..." – kein Datum, also heute annehmen
                # Wenn die Datei über Mitternacht geht, werden ältere Zeilen
                # anhand des Dateiänderungsdatums beurteilt
                try:
                    ts_str = line[:8]  # "HH:MM:SS"
                    h, m, s = int(ts_str[0:2]), int(ts_str[3:5]), int(ts_str[6:8])
                    line_time = datetime.combine(today, datetime.min.time().replace(hour=h, minute=m, second=s))
                    # Falls Zeitstempel > jetzt → war gestern
                    if line_time > datetime.now():
                        line_time -= timedelta(days=1)
                    if line_time >= cutoff:
                        kept.append(line)
                except (ValueError, IndexError):
                    # Zeile ohne gültigen Timestamp (Traceback etc.) → behalten
                    kept.append(line)
        # Nur schreiben wenn tatsächlich Zeilen entfernt wurden
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            original_count = sum(1 for _ in f)
        if len(kept) < original_count:
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(kept)
    except Exception:
        pass  # Log-Trimming darf niemals das Script blockieren

_trim_log(24)

# Expliziter Logger-Setup (basicConfig wird ignoriert wenn grpc/rumps
# bereits logging initialisiert haben)
# Millisekunden im Zeitstempel: Verzögerungs-Diagnosen (PTSL-Latenz, Lock-Stau)
# brauchen Sub-Sekunden-Auflösung, sonst sind Trigger→Aktion-Zeiten unmessbar.
_log_formatter = logging.Formatter("%(asctime)s.%(msecs)03d %(message)s", datefmt="%H:%M:%S")

_file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
_file_handler.setFormatter(_log_formatter)
_file_handler.setLevel(logging.INFO)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)
_console_handler.setLevel(logging.INFO)

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
# Alte Handler entfernen (falls basicConfig doch schon lief)
_root_logger.handlers.clear()
_root_logger.addHandler(_file_handler)
_root_logger.addHandler(_console_handler)

# ── Crash-Diagnose ──────────────────────────────────────────────────────────
# Native Abstürze (Segfault/ObjC-Abort) und unbehandelte Exceptions landen
# sonst nirgends: stderr geht im App-Bundle verloren, und der Prozess stirbt
# ohne Log-Eintrag (siehe Crash vom 2026-06-11 nach Interplay-Import).

# faulthandler schreibt bei SIGSEGV/SIGABRT/etc. die Python-Stacktraces aller
# Threads in diese Datei. Das Datei-Handle muss prozessweit offen bleiben.
CRASH_LOG_PATH = os.path.join(_LOG_DIR, "PunchBuddy_crash.log")
try:
    _crash_log_file = open(CRASH_LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(file=_crash_log_file)
except Exception:
    _crash_log_file = None


def _log_thread_exception(args):
    if args.exc_type is SystemExit:
        return
    _name = args.thread.name if args.thread is not None else "?"
    logging.error(f"Unbehandelte Exception in Thread '{_name}'",
                  exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


threading.excepthook = _log_thread_exception

_orig_sys_excepthook = sys.excepthook


def _log_sys_exception(exc_type, exc_value, exc_tb):
    if exc_type not in (SystemExit, KeyboardInterrupt):
        logging.error("Unbehandelte Exception (Main-Thread)",
                      exc_info=(exc_type, exc_value, exc_tb))
    _orig_sys_excepthook(exc_type, exc_value, exc_tb)


sys.excepthook = _log_sys_exception

