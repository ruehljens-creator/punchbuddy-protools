"""Export-Workflows: WAV, AAF, Interplay-Import/-Export, sowie die
PT-Log-/AppleScript-Hilfsfunktionen rund um die UI-Automation.
"""
import os
import re
import glob
import time
import select
import shutil
import logging
import threading
import subprocess

import rumps

from punchbuddy import state
from punchbuddy.i18n import t
from punchbuddy.config import load_settings, DEFAULT_SETTINGS
from punchbuddy.keys import (
    _send_key, _send_key_to_app, _activate_app, _pt_pid,
    _VK_F2, _VK_ESC, _VK_V, _VK_F13, _VK_RETURN,
)
from punchbuddy.engine import (
    _get_engine, _ptsl_call, ensure_preroll_on, restore_preroll,
    _close_engine, refresh_session_tracks, _invalidate_session_tracks,
    _get_cached_track_names, _show_error, _set_preroll_state, _get_preroll_status,
    _reset_if_rpc_error,
)
from punchbuddy.transport import _set_busy, _send_shift_oe
from punchbuddy.loudness import _run_loudness_with_progress, normalize_track
from punchbuddy.uikit import _show_progress_win, _dispatch_main

# Export-spezifisches Lock (verhindert parallele Export-Läufe)
_export_lock = threading.Lock()


def _resolve_export_dir(custom_path, session_dir):
    """Ermittelt den Ziel-Exportordner. Ein nicht-leerer custom_path wird
    verwendet (und bei Bedarf angelegt); sonst der Standard <session>/export.
    Fällt bei nicht anlegbarem Custom-Pfad auf den Standard zurück."""
    default_dir = os.path.join(session_dir, "export")
    custom = (custom_path or "").strip()
    if custom:
        try:
            os.makedirs(custom, exist_ok=True)
            return custom
        except Exception as e:
            logging.warning(f"  Custom-Exportpfad '{custom}' nicht nutzbar ({e}) – "
                            f"nutze Standard {default_dir}")
    os.makedirs(default_dir, exist_ok=True)
    return default_dir


def _detect_video_track(engine, settings=None):
    """Erkennt die Videospur automatisch via PTSL (type=4).
    Fallback auf settings['video_track'] wenn nichts gefunden."""
    fallback = (settings or {}).get("video_track", "Video 1")
    try:
        # Über _ptsl_call: serialisiert + verwirft die Engine bei totem Channel
        # (sonst bliebe eine Zombie-Verbindung bestehen).
        ok, tracks = _ptsl_call(engine.track_list, label="DetectVideoTrack", timeout=10.0)
        if not ok or tracks is None:
            logging.warning(f"Video-Spur: track_list fehlgeschlagen – Fallback: '{fallback}'")
            return fallback
        video_tracks = [t.name for t in tracks if t.type == 4]
        if video_tracks:
            name = video_tracks[0]
            logging.info(f"Video-Spur auto-erkannt: '{name}'")
            return name
        else:
            logging.warning(f"Keine Video-Spur (type=4) gefunden – Fallback: '{fallback}'")
            return fallback
    except Exception as e:
        logging.warning(f"Video-Spur Erkennung fehlgeschlagen: {e} – Fallback: '{fallback}'")
        return fallback


# Transport-Zustände, in denen Pro Tools aktiv läuft und ein Stop nötig ist,
# bevor exportiert werden darf. "TS_TransportIsStopping" ist NICHT dabei: PT
# stoppt dort bereits selbst – ein toggle_play_state würde es wieder starten.
_STOP_NEEDED_STATES = (
    "TS_TransportRecording", "TS_TransportPlaying",
    "TS_TransportIsCued", "TS_TransportIsCuedForPreview",
)


def _ensure_transport_stopped(engine, settle=2.0):
    """Stellt vor jedem Export sicher, dass Pro Tools nicht mehr aufnimmt/abspielt.

    Löst der User den Export aus, während noch eine Aufnahme läuft (ohne vorher
    zu stoppen), würde PT mitten im NEXIS-Schreibvorgang zum Consolidate
    gezwungen – das kann die Session beschädigen. Deshalb: läuft der Transport
    noch, Stop senden, auf bestätigten Stopp warten und PT `settle` Sekunden Zeit
    geben, die Dateien zu finalisieren. Steht der Transport bereits (User hat
    selbst gestoppt), kehrt die Funktion sofort zurück.

    Gibt True zurück, wenn der Transport (jetzt) steht, sonst False.
    """
    ok, ts = _ptsl_call(engine.transport_state, label="ExportTransportCheck", timeout=6.0)
    if not ok:
        logging.warning("  Export: Transport-State nicht lesbar – fahre vorsichtshalber fort.")
        return False

    s = str(ts)
    if s == "TS_TransportStopped":
        logging.info("  Export: Transport steht bereits – kein Stop nötig.")
        return True

    if s in _STOP_NEEDED_STATES:
        logging.info(f"  Export: Transport läuft ({s}) – sende Stop vor Export.")
        _ptsl_call(engine.toggle_play_state, label="ExportStop", timeout=6.0)
    else:  # TS_TransportIsStopping o.ä. – PT stoppt schon selbst
        logging.info(f"  Export: Transport stoppt bereits ({s}) – warte auf Abschluss.")

    # Auf bestätigten Stopp warten – mit MINIMALEN PTSL-Calls: Pro Tools
    # beantwortet PTSL bimodal (10ms ODER 300–1400ms pro Call, gemessen
    # 2026-07-21, auch auf leerer Session). Der alte 0.5s-Poll-Loop feuerte bis
    # zu 20 Calls und stapelte so mehrere Sekunden Wartezeit auf, die über den
    # globalen _ptsl_lock ALLE anderen Befehle (Stream-Deck-Trigger!) blockierte.
    # Gestaffelte Kontrollen: erste nach 0.5s (schneller Stop bleibt schnell,
    # gemessen identisch zum alten Verhalten), danach wachsende Abstände →
    # max. 4 Kontroll-Reads statt 20. Semantik unverändert: bestätigter
    # Stopp + Settle. PT bestätigt den Stop im Studio gemessen nach ~2.6s.
    for _pause in (0.5, 1.0, 1.5, 2.0):
        time.sleep(_pause)
        ok2, ts2 = _ptsl_call(engine.transport_state, label="ExportStopWait", timeout=4.0)
        if ok2 and str(ts2) == "TS_TransportStopped":
            logging.info(f"  Export: Transport gestoppt – warte {settle:.0f}s auf Datei-Finalisierung.")
            time.sleep(settle)
            return True

    logging.warning("  Export: Stop nicht bestätigt – warte Settle und fahre trotzdem fort.")
    time.sleep(settle)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Hauptautomatisierung
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# Interplay Export (UI-Automation)
# ─────────────────────────────────────────────────────────────────────────────

def _find_newest_pt_log():
    """Returns path to the newest Pro Tools log file, or None."""
    import glob as _glob
    files = _glob.glob(os.path.expanduser("~/Library/Logs/Avid/Pro_Tools_*.txt"))
    return max(files, key=os.path.getmtime) if files else None


def _poll_pt_log_for_export(log_path, start_pos, timeout=90):
    """
    Watches PT log for MediaCentral export result.
    Uses kqueue (instant file-write notification on macOS) with 0.3s polling as fallback.
    Returns (success: True/False/None, message: str).
    """
    SUCCESS_MARKER = "The selected tracks were successfully placed into the Pro Tools sequence"
    # PT loggt das Export-Ergebnis als Dialog-Zeile:
    #   "SLnk_MachineMgr::SendDialogOnScreen: <id>, error msg = <Text>"
    # Die <id> ist eine instabile Instanz-Nummer (NICHT fest verdrahten!),
    # daher matchen wir nur den stabilen Prefix und werten den Klartext aus.
    DIALOG_MARKER  = "SendDialogOnScreen"

    if not log_path:
        return None, "PT Log nicht verfügbar"

    def _check(chunk):
        if SUCCESS_MARKER in chunk:
            return True, "Export erfolgreich (PT Log)"
        for line in chunk.splitlines():
            if DIALOG_MARKER not in line or "error msg =" not in line:
                continue
            msg = line.split("error msg =", 1)[1].strip()
            if not msg:
                continue  # leeres Dialog-Clear (Schließen) – kein Ergebnis
            if "successfully" in msg.lower():
                return True, "Export erfolgreich (PT Log)"
            return False, f"Export-Dialog: {msg[:120]}"
        return None, None

    deadline = time.time() + timeout
    active_log, cur_pos = log_path, start_pos

    # ── kqueue: sofortige Datei-Event-Benachrichtigung (macOS) ───────────────
    try:
        fd = os.open(active_log, os.O_RDONLY | os.O_NONBLOCK)
        kq = select.kqueue()
        try:
            ke = select.kevent(fd,
                               filter=select.KQ_FILTER_VNODE,
                               flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                               fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
            while time.time() < deadline:
                remaining = deadline - time.time()
                events = kq.control([ke], 4, min(remaining, 2.0))

                # Log-Rotation prüfen (PT startet neues Log)
                newest = _find_newest_pt_log()
                if newest and newest != active_log:
                    logging.debug(f"  kqueue: neues PT-Log → {newest}")
                    os.close(fd); kq.close()
                    active_log, cur_pos = newest, 0
                    fd = os.open(active_log, os.O_RDONLY | os.O_NONBLOCK)
                    kq = select.kqueue()
                    ke = select.kevent(fd,
                                       filter=select.KQ_FILTER_VNODE,
                                       flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                                       fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
                    continue

                if not events:
                    continue

                try:
                    sz = os.path.getsize(active_log)
                    if sz > cur_pos:
                        with open(active_log, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(cur_pos)
                            chunk = f.read()
                            cur_pos = f.tell()
                        ok, msg = _check(chunk)
                        if ok is not None:
                            return ok, msg
                except Exception as e:
                    logging.debug(f"  kqueue read: {e}")
        finally:
            try: os.close(fd)
            except: pass
            try: kq.close()
            except: pass
        return None, "PT Log Timeout – Ergebnis unbekannt"

    except Exception as e:
        logging.debug(f"  kqueue nicht verfügbar ({e}) – nutze Polling")

    # ── Polling-Fallback (0.3s Intervall) ────────────────────────────────────
    while time.time() < deadline:
        try:
            newest = _find_newest_pt_log()
            if newest and newest != active_log:
                active_log, cur_pos = newest, 0
            if active_log and os.path.exists(active_log) and os.path.getsize(active_log) > cur_pos:
                with open(active_log, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(cur_pos)
                    chunk = f.read()
                    cur_pos = f.tell()
                ok, msg = _check(chunk)
                if ok is not None:
                    return ok, msg
        except Exception as e:
            logging.debug(f"  PT Log Poll: {e}")
        time.sleep(0.3)

    return None, "PT Log Timeout – Ergebnis unbekannt"


def _wait_for_pt_modal_dismissed(log_path, log_pos, timeout=5.0):
    """
    Wartet via kqueue bis PT den Modal-Dialog vollständig verarbeitet hat.
    Sobald der Dialog geschlossen wird, schreibt PT eine weitere
    'SLnk_MachineMgr::SendDialogOnScreen: <id>'-Zeile (Dialog-Statuswechsel).
    Die <id> ist eine instabile Instanz-Nummer – daher nur der stabile Prefix.
    Zuverlässiger als CGWindowList (funktioniert auch bei Sheets/Panel-Dialogen).
    """
    DISMISS_MARKER = "SendDialogOnScreen"
    if not log_path or not os.path.exists(log_path):
        time.sleep(1.0)
        return

    deadline = time.time() + timeout
    cur_pos = log_pos
    try:
        fd = os.open(log_path, os.O_RDONLY | os.O_NONBLOCK)
        kq = select.kqueue()
        try:
            ke = select.kevent(fd,
                               filter=select.KQ_FILTER_VNODE,
                               flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE | select.KQ_EV_CLEAR,
                               fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
            while time.time() < deadline:
                remaining = deadline - time.time()
                events = kq.control([ke], 4, min(remaining, 1.0))
                if events:
                    sz = os.path.getsize(log_path)
                    if sz > cur_pos:
                        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                            f.seek(cur_pos)
                            chunk = f.read()
                            cur_pos = f.tell()
                        if DISMISS_MARKER in chunk:
                            logging.info("  Interplay: Modal geschlossen (PT Log)")
                            time.sleep(0.1)
                            return
        finally:
            try: os.close(fd)
            except: pass
            try: kq.close()
            except: pass
    except Exception as e:
        logging.debug(f"  PT Modal-Dismiss kqueue: {e}")

    logging.debug("  Interplay: Modal-Dismiss Timeout – weiter")
    time.sleep(0.3)
def _run_applescript(script, timeout=30):
    """Fuehrt ein AppleScript aus und gibt (returncode, stdout, stderr) zurueck.
    timeout: Sekunden bis zum Abbruch des osascript-Prozesses. Für AAF-Exporte
    großzügig wählen – das Einbetten/Schreiben großer Medien dauert lange."""
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

def _read_pt_export_result(timeout=12, settings=None):
    """Liest den Inhalt des Pro Tools Bestätigungsfensters nach dem Export.
    Überspringt Hauptfenster (Edit/Mix/Session).
    Gibt (window_title, window_text, success) zurück.
    success=True  → Erfolgs-Keywords gefunden
    success=False → Fehler-Keywords gefunden
    success=None  → kein eindeutiges Ergebnis (Fenster nicht lesbar / kein Keyword)"""

    script = '''
        tell application "System Events"
            tell process "Pro Tools"
                repeat with w in windows
                    try
                        set wTitle to name of w
                        if wTitle does not contain "Edit:" and ¬
                           wTitle does not contain "Mix:" and ¬
                           wTitle does not contain "Transport" and ¬
                           wTitle is not "" then
                            set textContent to ""
                            try
                                repeat with st in (every static text of w)
                                    try
                                        set v to value of st
                                        if v is not missing value and v is not "" then
                                            set textContent to textContent & v & " | "
                                        end if
                                    end try
                                end repeat
                            end try
                            return wTitle & "|||" & textContent
                        end if
                    end try
                end repeat
                return "NO_DIALOG|||"
            end tell
        end tell
    '''

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = _run_applescript(script)
            if rc == 0 and "|||" in out:
                parts = out.split("|||", 1)
                title = parts[0].strip()
                text  = parts[1].strip() if len(parts) > 1 else ""
                if title and title != "NO_DIALOG":
                    combined = (title + " " + text).lower()
                    # Keywords aus Settings laden, Fallback auf Defaults
                    _s = settings or {}
                    _err_raw = _s.get("export_error_keywords",
                        "error,fail,fehler,unsuccessful,could not,unable,problem,warning,aborted,abgebrochen")
                    _suc_raw = _s.get("export_success_keywords",
                        "success,complete,finished,done,exported,erfolgreich,abgeschlossen,fertig")
                    error_kw   = [k.strip().lower() for k in _err_raw.split(",") if k.strip()]
                    success_kw = [k.strip().lower() for k in _suc_raw.split(",") if k.strip()]
                    if any(k in combined for k in error_kw):
                        return title, text, False
                    if any(k in combined for k in success_kw):
                        return title, text, True
                    return title, text, None   # Fenster gefunden, Ergebnis unklar
        except Exception:
            pass
        time.sleep(0.1)
    return "", "", None   # Kein Fenster gefunden


def _wait_for_pt_window(title_contains, timeout=30, gone=False):
    """
    Wartet bis ein Pro Tools Fenster mit dem Titel erscheint (gone=False)
    oder verschwindet (gone=True). Gibt True zurueck bei Erfolg.
    """
    script = f'''
        tell application "System Events"
            tell process "Pro Tools"
                set wNames to name of every window
            end tell
        end tell
        return wNames as text
    '''
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, out, _ = _run_applescript(script)
            if rc == 0:
                found = title_contains.lower() in out.lower()
                if (not gone and found) or (gone and not found):
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _tc_seconds(tc):
    """Timecode 'hh:mm:ss:ff(.xx)' → Sekunden (Frames werden ignoriert)."""
    try:
        h, m, s = (int(x) for x in tc.split(".")[0].split(":")[:3])
        return h * 3600 + m * 60 + s
    except Exception:
        return None


def _consolidate_timeout(in_time, out_time, base=60.0, per_audio_second=0.5, cap=600.0):
    """Wartezeit fürs Consolidate-Fenster anhand der Selektionslänge.
    Lange Selektionen brauchen auf NEXIS deutlich mehr als die früher fest
    codierten 60s (Log 2026-06-11: 5:38-min-Session × 7 Spuren ≈ 80s)."""
    a, b = _tc_seconds(in_time), _tc_seconds(out_time)
    if a is None or b is None or b <= a:
        return base
    return min(cap, base + (b - a) * per_audio_second)


# Stichwörter, mit denen das Consolidate-Fortschrittsfenster BEGINNT.
# Wichtig: Vergleich auf Wortanfang, NICHT "enthält" – sonst matcht ein
# Sessionname wie "…Consolidated.01_forPT_1" im Edit-Fenstertitel fälschlich
# und der Wait läuft in den vollen Timeout (Log 2026-06-11: 80s statt ~4s).
_CONSOLIDATE_TITLE_PREFIXES = ("consolidat", "zusammenführ", "consolida", "consolidando")
_WINDOW_NAME_SEP = "|||"


def _pt_window_names():
    """Liefert die Fenstertitel von Pro Tools als Liste (oder None bei Fehler)."""
    script = f'''
        set AppleScript's text item delimiters to "{_WINDOW_NAME_SEP}"
        tell application "System Events"
            tell process "Pro Tools"
                set wNames to name of every window
            end tell
        end tell
        return wNames as text
    '''
    rc, out, _ = _run_applescript(script)
    if rc != 0:
        return None
    return out.split(_WINDOW_NAME_SEP)


def _has_consolidate_window(names):
    """True wenn ein Fenstertitel mit einem Consolidate-Stichwort BEGINNT."""
    for n in names:
        low = n.strip().lower()
        if low.startswith(_CONSOLIDATE_TITLE_PREFIXES):
            return True
    return False


def _wait_for_consolidate_window_gone(timeout=60):
    """
    Wartet bis das Consolidate-Fortschrittsfenster ("Consolidating",
    "Zusammenführen" …) verschwindet. Persistente Fenster (Edit/Mix) werden
    ignoriert, auch wenn der Sessionname ein Stichwort enthält.
    """
    time.sleep(0.5)  # Kurz warten, damit das Fenster Zeit hat sich zu öffnen

    start = time.time()
    deadline = start + timeout
    last_log = -10.0
    while time.time() < deadline:
        try:
            names = _pt_window_names()
            if names is not None:
                if not _has_consolidate_window(names):
                    logging.info("  Consolidate-Fenster nicht mehr aktiv.")
                    return True
                elapsed = time.time() - start
                if elapsed - last_log >= 5.0:
                    logging.info(f"  Warte auf Abschluss des Consolidate-Vorgangs in Pro Tools... ({int(elapsed)}s)")
                    last_log = elapsed
        except Exception as e:
            logging.debug(f"Fehler bei _wait_for_consolidate_window_gone: {e}")
        time.sleep(0.5)
    logging.warning(f"  Timeout ({int(timeout)}s) beim Warten auf das Consolidate-Fenster.")
    return False

def _wait_for_consolidated_files(session_dir, track_names, timeout=10):
    """
    Wartet bis die vom Consolidate geschriebenen .wav-Dateien stabil sind
    (Größe > 0, unverändert über 0.5s).

    Zwei Fallstricke der alten Version sind hier behoben:
    - Der Stabilitäts-Timer wurde bei jeder Prüfung zurückgesetzt, solange
      noch keine 0.2s vergangen waren – bei 0.1s Poll-Intervall konnte er
      dadurch NIE auslösen und der Timeout lief immer komplett durch.
    - Spuren ohne Material erzeugen keine Datei; gewartet wird deshalb nur
      auf Dateien, die tatsächlich existieren.
    """
    audio_dir = os.path.join(session_dir, "Audio Files")
    if not os.path.isdir(audio_dir):
        return False

    logging.info("  Warte auf Stabilität der exportierten Audiodateien...")
    now = time.time()
    deadline = now + timeout
    grace_deadline = now + 1.5   # Zeit für verspätet erscheinende Dateien

    # Letzte bekannte Dateigröße + Zeitpunkt der letzten Größen-Änderung
    last_states = {}

    def _latest_wav(track_name):
        latest_file, latest_mtime = None, -1.0
        try:
            for f in os.listdir(audio_dir):
                base = os.path.splitext(f)[0]
                if (base == track_name or
                        base.startswith(track_name + "_") or
                        base.startswith(track_name + ".") or
                        base.startswith(track_name + "-")) and f.lower().endswith(".wav"):
                    full = os.path.join(audio_dir, f)
                    mtime = os.path.getmtime(full)
                    if mtime > latest_mtime:
                        latest_mtime, latest_file = mtime, full
        except Exception:
            return None
        return latest_file

    while time.time() < deadline:
        found = 0
        stable = 0
        for track_name in track_names:
            latest_file = _latest_wav(track_name)
            if not latest_file:
                continue
            found += 1
            try:
                size = os.path.getsize(latest_file)
            except Exception:
                size = 0
            prev_size, prev_time = last_states.get(track_name, (None, None))
            now = time.time()
            if size != prev_size:
                last_states[track_name] = (size, now)
            elif size > 0 and (now - prev_time) >= 0.5:
                stable += 1

        # Erfolg: Grace-Periode vorbei, mind. eine Datei da, alle gefundenen stabil
        if time.time() >= grace_deadline and found > 0 and stable == found:
            missing = len(track_names) - found
            if missing:
                logging.info(f"  Exportierte Audiodateien stabil ({found} Datei(en); "
                             f"{missing} Spur(en) ohne Datei im Bereich).")
            else:
                logging.info("  Alle exportierten Audiodateien sind stabil und vollständig.")
            return True

        time.sleep(0.1)

    logging.warning("  Erreichte Timeout beim Warten auf stabile Audiodateien – fahre fort.")
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Interplay Import (Avid Interplay Access → Pro Tools)
# ─────────────────────────────────────────────────────────────────────────────
_import_lock = threading.Lock()

def _run_applescript_safe(script, timeout=30, label=""):
    """Führt AppleScript aus, fängt Timeouts sauber ab. Gibt (ok, stdout) zurück."""
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout
        )
        out = proc.stdout.strip()
        if proc.returncode != 0:
            err = proc.stderr.strip()
            logging.warning(f"  [{label}] AppleScript returncode {proc.returncode}: {err}")
        return True, out
    except subprocess.TimeoutExpired:
        logging.warning(f"  [{label}] AppleScript Timeout nach {timeout}s")
        return False, ""
    except Exception as e:
        logging.error(f"  [{label}] AppleScript Fehler: {e}")
        return False, ""

def run_interplay_import():
    """Führt den Interplay Import (Avid Interplay Access → Pro Tools) schrittweise aus."""
    with _import_lock:
        if state.import_running:
            logging.warning("Import laeuft bereits – ignoriert.")
            return
        state.import_running = True
    prog = None
    _set_busy(True)

    try:
        logging.info("=== INTERPLAY IMPORT START ===")
        prog = _show_progress_win(t("prog_title_import"))
        prog["update"](0.03, t("prog_check_session"))

        # ── Schritt 0: Offene Session schließen (falls aktiviert) ─────
        settings = load_settings()
        if settings.get("import_close_session", True):
            logging.info("  Import Schritt 0: Prüfe ob eine Session offen ist...")
            try:
                engine = _get_engine()
                if engine is not None:
                    # Alle Calls über _ptsl_call: Lock + gRPC-Deadline +
                    # Latenz-Instrumentierung. Rohe engine.*-Aufrufe konnten bei
                    # totem Channel haengen und blockierten den Import-Start
                    # unsichtbar (PTSL antwortet bimodal, bis ~1.4s pro Call).
                    ok_n, sess_name = _ptsl_call(engine.session_name,
                                                 label="ImportSessionCheck", timeout=5.0)
                    if ok_n and sess_name:
                        prog["update"](0.08, t("prog_close_session").format(sess_name))
                        logging.info(f"  Session '{sess_name}' ist offen – wird gespeichert und geschlossen...")
                        ok_s, _ = _ptsl_call(engine.save_session,
                                             label="ImportSessionSave", timeout=30.0)
                        if ok_s:
                            logging.info(f"  Session '{sess_name}' gespeichert.")
                        else:
                            logging.warning("  Session-Speichern nicht bestätigt – schließe trotzdem.")
                        _ptsl_call(lambda: engine.close_session(save_on_close=True),
                                   label="ImportSessionClose", timeout=30.0)
                        logging.info(f"  Session '{sess_name}' geschlossen.")
                        # Singleton zurücksetzen: PTSL-Verbindung zur geschlossenen
                        # Session ist ungültig – neue Session braucht neue Verbindung
                        _close_engine()
                        time.sleep(1.5)  # Pro Tools braucht kurz zum Schließen
                    else:
                        # session_name() wirft/scheitert, wenn keine Session offen
                        logging.info("  Keine Session offen – überspringe.")
                else:
                    logging.info("  PTSL Engine nicht verfügbar – überspringe Session-Check.")
            except Exception as e:
                logging.warning(f"  Session-Check fehlgeschlagen: {e} – fahre trotzdem fort.")

        # ── Schritt 1: Interplay Access – Sequenzname kopieren ────────
        # Nutzt CGEvent statt AppleScript System Events (keine Accessibility-Probleme)
        _VK_1 = 18   # '1' (lokal, da nur hier gebraucht)

        prog["update"](0.15, t("prog_copy_seq_name"))
        logging.info("  Import Schritt 1: Interplay Access – Sequenzname kopieren...")
        _activate_app("interplayAccess")
        try:
            _run_applescript(
                'tell application "System Events" to '
                'set frontmost of (first process whose name contains "Interplay") to true'
            )
        except Exception:
            pass
        time.sleep(0.8)

        # F2 → Cmd+A → Cmd+C (via AppleScript System Events, damit Java-Clipboard
        # mit macOS-Systemclipboard synchronisiert wird) → ESC
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to key code 120'  # F2
        )
        time.sleep(0.6)
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to keystroke "a" using command down'
        )
        time.sleep(0.3)
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to keystroke "c" using command down'
        )
        time.sleep(0.5)
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to key code 53'  # ESC
        )
        time.sleep(0.3)

        try:
            seq_name = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=2
            ).stdout.strip()
            if seq_name:
                logging.info(f"  Import Schritt 1: Sequenzname: '{seq_name}'")
            else:
                logging.warning("  Import Schritt 1: Clipboard leer – F2/Cmd+C hat nicht funktioniert.")
        except Exception:
            seq_name = ""

        # Cmd+Shift+P via AppleScript: Java-Menüaktion wird so zuverlässig ausgelöst
        prog["update"](0.35, t("prog_send_seq_to_pt"))
        _run_applescript(
            'tell application "System Events" to tell process "interplayAccess" '
            'to keystroke "p" using {command down, shift down}'
        )
        logging.info("  Import Schritt 1 OK.")
        time.sleep(1.0)

        # ── Schritt 2: Auf "… (offline)"-Fenster warten, Name einfügen ──
        # Pro Tools öffnet nach Cmd+Shift+P aus Interplay ein Fenster dessen
        # Titel sich mit jedem PT-Update ändert ("ProTools Ultimate 2026.4 (offline)").
        # "(offline)" ist der versionsstabile Substring – darauf warten statt blind schlafen.
        prog["update"](0.43, t("prog_wait_pt_dialog"))
        logging.info("  Import Schritt 2: Warte auf Pro Tools '(offline)'-Dialog...")
        _activate_app("Pro Tools")
        if _wait_for_pt_window("offline", timeout=30):
            logging.info("  Import Schritt 2: Fenster gefunden.")
        else:
            logging.warning("  Import Schritt 2: '(offline)'-Fenster nach 30s nicht erschienen – versuche trotzdem.")
        prog["update"](0.55, t("prog_paste_session_path"))
        time.sleep(0.3)
        _send_key(_VK_V, cmd=True)  # Sequenzname einfügen
        time.sleep(0.3)
        _send_key(_VK_RETURN)       # Bestätigen / Fenster schließen
        logging.info("  Import Schritt 2 OK.")

        # ── Schritt 3: Auf "Import Session Data" Fenster warten ───────
        prog["update"](0.62, t("prog_wait_import_session_data"))
        logging.info("  Import Schritt 3: Warte auf 'Import Session Data'...")
        if not _wait_for_pt_window("Import Session Data", timeout=60):
            logging.error("  Import Schritt 3 FEHLER: 'Import Session Data' nicht erschienen.")
            return
        logging.info("  Import Schritt 3 OK – Fenster gefunden.")
        time.sleep(0.5)

        # ── Schritt 4: Match All + Playlist-Option setzen ─────────────
        # (Button-Clicks via AppleScript – braucht KEIN keystroke-Permission)
        prog["update"](0.70, t("prog_set_import_options"))
        logging.info("  Import Schritt 4: Match All + Playlist-Option...")
        ok, out = _run_applescript_safe('''
            try
                tell application "System Events"
                    tell process "Pro Tools"
                        set frontmost to true
                        set importWin to window "Import Session Data"

                        -- Match All
                        try
                            click button "Match All" of importWin
                        end try
                        delay 0.5

                        -- Playlist-Option: Import - Overlay New On Existing Playlists
                        set foundOption to false
                        try
                            click pop up button "Main Playlist Options" of importWin
                            delay 0.2
                            click menu item "Import - Overlay New On Existing Playlists" of menu 1 of pop up button "Main Playlist Options" of importWin
                            set foundOption to true
                        on error
                            try
                                click radio button "Import - Overlay New On Existing Playlists" of importWin
                                set foundOption to true
                            on error
                                try
                                    click button "Import - Overlay New On Existing Playlists" of importWin
                                    set foundOption to true
                                end try
                            end try
                        end try

                        if not foundOption then
                            set allPopUps to pop up buttons of importWin
                            repeat with p in allPopUps
                                try
                                    click p
                                    delay 0.2
                                    if exists menu item "Import - Overlay New On Existing Playlists" of menu 1 of p then
                                        click menu item "Import - Overlay New On Existing Playlists" of menu 1 of p
                                        set foundOption to true
                                        exit repeat
                                    else
                                        key code 53
                                        delay 0.1
                                    end if
                                end try
                            end repeat
                        end if

                        if not foundOption then
                            set allGroups to groups of importWin
                            repeat with g in allGroups
                                set groupPopUps to pop up buttons of g
                                repeat with p in groupPopUps
                                    try
                                        click p
                                        delay 0.2
                                        if exists menu item "Import - Overlay New On Existing Playlists" of menu 1 of p then
                                            click menu item "Import - Overlay New On Existing Playlists" of menu 1 of p
                                            set foundOption to true
                                            exit repeat
                                        else
                                            key code 53
                                            delay 0.1
                                        end if
                                    end try
                                end repeat
                                if foundOption then exit repeat
                                try
                                    click radio button "Import - Overlay New On Existing Playlists" of g
                                    set foundOption to true
                                    exit repeat
                                end try
                                try
                                    click button "Import - Overlay New On Existing Playlists" of g
                                    set foundOption to true
                                    exit repeat
                                end try
                            end repeat
                        end if
                    end tell
                end tell
                if foundOption then
                    return "OK"
                else
                    return "WARN:Playlist-Option nicht gefunden"
                end if
            on error errMsg
                return "ERROR:" & errMsg
            end try
        ''', timeout=20, label="MatchAll")
        if "ERROR" in out:
            logging.error(f"  Import Schritt 4 FEHLER: {out}")
            return
        if "WARN" in out:
            logging.warning(f"  Import Schritt 4: {out}")
        else:
            logging.info("  Import Schritt 4 OK.")
        time.sleep(0.3)

        # ── Schritt 5: Choose + Track Data Preset ─────────────────────
        # (Button-Clicks via AppleScript, Preset-Shortcut via CGEvent)
        logging.info("  Import Schritt 5: Choose + Track Data Preset...")
        ok, out = _run_applescript_safe('''
            try
                tell application "System Events"
                    tell process "Pro Tools"
                        set frontmost to true
                        set importWin to window "Import Session Data"

                        -- Choose... Button
                        try
                            click button "Choose..." of importWin
                        on error
                            set allGroups to groups of importWin
                            repeat with g in allGroups
                                try
                                    click button "Choose..." of g
                                    exit repeat
                                end try
                            end repeat
                        end try
                    end tell
                end tell
                return "OK"
            on error errMsg
                return "ERROR:" & errMsg
            end try
        ''', timeout=10, label="ChooseBtn")
        if "ERROR" in out:
            logging.warning(f"  Import Schritt 5a (Choose): {out}")

        # Warte auf Track Data Fenster
        prog["update"](0.80, t("prog_choose_preset"))
        logging.info("  Import Schritt 5: Warte auf Track Data Fenster...")
        if _wait_for_pt_window("Track Data to Import", timeout=15):
            time.sleep(0.3)
            # Preset 1 via Ctrl+1 (CGEvent)
            _send_key_to_app(_VK_1, "Pro Tools", ctrl=True)
            time.sleep(0.3)
            _send_key(_VK_RETURN)  # Confirm
            logging.info("  Import Schritt 5 OK.")
        else:
            logging.warning("  Import Schritt 5: Track Data Fenster nicht gefunden.")
        time.sleep(1.0)

        # ── Schritt 6: Import bestätigen (CGEvent) ────────────────────
        prog["update"](0.87, t("prog_confirm_import"))
        logging.info("  Import Schritt 6: Import bestätigen...")
        time.sleep(0.3)
        _send_key(_VK_RETURN)
        logging.info("  Import Schritt 6 OK – Import gestartet.")

        # ── Schritt 7: Warte bis Import Session Data Fenster weg ──────
        prog["update"](0.90, t("prog_import_running"))
        logging.info("  Import Schritt 7: Warte auf Abschluss (max 120s)...")
        if _wait_for_pt_window("Import Session Data", timeout=120, gone=True):
            logging.info("  Import Schritt 7 OK – Import Session Data geschlossen.")
        else:
            logging.warning("  Import Schritt 7: Fenster nach 120s noch offen.")

        # ── Schritt 8: Warte bis Pro Tools fertig geladen hat ─────────
        prog["update"](0.95, t("prog_pt_loading"))
        logging.info("  Import Schritt 8: Warte bis Pro Tools bereit ist...")
        time.sleep(2.0)
        for i in range(180):
            ok, out = _run_applescript_safe('''
                try
                    tell application "System Events"
                        tell process "Pro Tools"
                            if exists window 1 then
                                set winTitle to name of window 1
                                if winTitle is not missing value then
                                    if winTitle contains "Importing" or winTitle contains "Processing" or winTitle contains "Task" or winTitle contains "Missing" or winTitle contains "Restoring" then
                                        return "BUSY"
                                    end if
                                end if
                                set progIndicators to (every UI element of window 1 whose role is "AXProgressIndicator")
                                if (count of progIndicators) > 0 then
                                    return "BUSY"
                                end if
                            end if
                        end tell
                    end tell
                    return "READY"
                on error
                    return "READY"
                end try
            ''', timeout=5, label="WaitReady")
            if not ok or out != "BUSY":
                break
            if i % 10 == 0 and i > 0:
                logging.info(f"  Import Schritt 8: Pro Tools noch beschäftigt ({i}s)...")
            time.sleep(1.0)
        logging.info("  Import Schritt 8 OK – Pro Tools bereit.")

        prog["update"](1.0, t("prog_import_done"))
        time.sleep(0.8)

        # ── Post-Import: Record-Tracks armen (NEXIS warm halten) ─────────
        # Engine reset + frisch verbinden damit wir die neue Session sehen.
        # Tracks armed lassen → NEXIS hält Dateien offen → kein Cold-Start
        # beim nächsten Record-Trigger, auch nach längerer Idle-Zeit.
        try:
            _close_engine()
            _eng_post = _get_engine()
            if _eng_post is not None:
                _cfg_post = state.app_ref.settings if state.app_ref is not None else load_settings()
                _arm_tracks = []
                for _key in ("tracks", "tracks_b"):
                    for _tr in _cfg_post.get(_key, []):
                        if _tr and _tr not in _arm_tracks:
                            _arm_tracks.append(_tr)
                _pt_post = _get_cached_track_names(_eng_post)
                _arm_want = sorted(tr for tr in _arm_tracks if _pt_post and tr in _pt_post)
                if _arm_want:
                    _ok_arm, _ = _ptsl_call(
                        _eng_post.set_track_record_enable_state, _arm_want, True,
                        label="PostImportArm", timeout=20.0
                    )
                    if _ok_arm:
                        logging.info(f"Post-Import: Tracks armed (NEXIS warm): {_arm_want}")
                    else:
                        logging.warning("Post-Import: Track arm fehlgeschlagen (unkritisch).")
                else:
                    logging.info("Post-Import: Keine passenden Record-Tracks zum Armen gefunden.")
        except Exception as _e_arm:
            logging.warning(f"Post-Import Arm fehlgeschlagen (unkritisch): {_e_arm}")

        logging.info("=== INTERPLAY IMPORT ENDE ===")
    except Exception as e:
        logging.error(f"Import Fehler: {e}", exc_info=True)
        _reset_if_rpc_error(e)  # toten gRPC-Channel verwerfen → kein Zombie
        if prog: prog["update"](1.0, f"{t('alert_error')}: {e}")
    finally:
        if prog: prog["close"]()
        # Engine nach Import schließen: Nächster Aufruf verbindet sich frisch.
        # Tracks bleiben in PT armed – NEXIS-Dateien bleiben offen.
        _close_engine()
        with _import_lock:
            state.import_running = False
        _set_busy(False)


def run_interplay_export(export_tracks, settings, workspace_steps=17):
    """
    Interplay Export mit vorgelagertem Consolidate + Loudness:
    1. Consolidate + Trim + Loudness (wie WAV/AAF)
    2. F13 → "Selected Tracks to Sequence in Production Management..."
    3. "Export Comment" → Enter
    4. "Export Options" → Select Workspace + Pfeil-Hoch × N → Enter
    5. "Processing Audio..." abwarten
    6. Bestaetigungsmeldung → Enter
    """
    _VK_UP = 126  # Arrow Up (lokal, da nur hier gebraucht)

    prog = None
    _set_busy(True)
    try:
        logging.info("=== INTERPLAY EXPORT START ===")
        prog = _show_progress_win(t("prog_title_interplay"))
        prog["update"](0.03, t("prog_connect_pt"))

        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfuegbar – Abbruch.")
            return False

        session_path = engine.session_path()
        session_dir = os.path.dirname(session_path)
        # PT liefert nach PTSL-Reconnect manchmal den Backup-Pfad
        if os.path.basename(session_dir) == "Session File Backups":
            session_dir = os.path.dirname(session_dir)
            logging.debug(f"  session_dir korrigiert (Session File Backups entfernt): {session_dir}")

        # ── Transport stoppen falls noch Aufnahme/Wiedergabe läuft ──
        prog["update"](0.05, t("prog_prep_tracks"))
        _ensure_transport_stopped(engine)

        # ── Versteckte Spuren einblenden ─────────────────────────────
        prog["update"](0.06, t("prog_prep_tracks"))
        logging.info(f"  Interplay: Spuren einblenden: {export_tracks}...")
        try:
            engine.set_track_hidden_state(export_tracks, False)
            time.sleep(0.25)
        except Exception as e:
            logging.warning(f"  Spuren einblenden: {e}")

        # ── Vorbereitung: Consolidate + Trim + Loudness ──────────────
        video_track = _detect_video_track(engine, settings)
        in_time = settings.get("export_start_tc", "10:00:00:00") + ".00"

        # Video-Ende ermitteln
        prog["update"](0.10, t("prog_get_video_end"))
        logging.info(f"  Interplay: Spuren selektieren: {export_tracks}")
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.3)

        video_end = None
        try:
            engine.select_all_clips_on_track(video_track)
            time.sleep(0.3)
            video_sel = engine.get_timeline_selection()
            video_end = video_sel[1]
            logging.info(f"  Interplay: Video: {video_sel[0]} -> {video_end}")
        except Exception as e:
            logging.error(f"  Interplay: Fehler Videospur '{video_track}': {e}")

        if not video_end or video_end == "00:00:00:00.00":
            logging.error("  Interplay: Video-Ende nicht ermittelt – überspringe Consolidate.")
        else:
            # Pro Tools Selection State vorbereiten und absichern
            with _protools_selection_context(engine):
                # Überhänge trimmen (vor dem Consolidate, damit der Clip danach ein physischer Haupt-Clip bleibt)
                prog["update"](0.15, t("prog_trim_overhangs"))
                _trim_overhangs(engine, export_tracks, in_time, video_end)

                # Export-Spuren selektieren und Timeline setzen für Consolidate
                engine.select_tracks_by_name(export_tracks)
                time.sleep(0.25)
                logging.info(f"  Interplay: Timeline: {in_time} -> {video_end}")
                engine.set_timeline_selection(in_time=in_time, out_time=video_end)
                time.sleep(0.25)
                # Auswahl auf die Export-Spuren ausdehnen
                engine.extend_selection_to_target_tracks(export_tracks)
                time.sleep(0.3)

                # Consolidate
                prog["update"](0.25, t("prog_consolidate"))
                logging.info("  Interplay: Consolidate...")
                engine.consolidate_clip()
                _wait_for_consolidate_window_gone(timeout=_consolidate_timeout(in_time, video_end))
                _wait_for_consolidated_files(session_dir, export_tracks, timeout=10)
                logging.info("  Interplay: Consolidate OK")

                # Loudness-Korrektur
                if settings.get("loudness_enabled", True):
                    prog["update"](0.30, t("prog_loudness"))
                    loud_tracks = settings.get("loudness_tracks", ["ST"])
                    target_lufs = settings.get("target_lufs", -23.0)
                    max_tp = settings.get("max_truepeak", -3.0)
                    _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp)

            logging.info("  Interplay: Vorbereitung (Consolidate+Loudness) abgeschlossen.")

        # ── DMGs auswerfen (verschieben den Workspace-Count) ─────────
        prog["update"](0.62, t("prog_eject_dmgs"))
        try:
            result = subprocess.run(
                ["hdiutil", "info", "-plist"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                import plistlib
                info = plistlib.loads(result.stdout.encode())
                ejected = []
                for img in info.get("images", []):
                    img_path = img.get("image-path", "")
                    for entity in img.get("system-entities", []):
                        mount_point = entity.get("mount-point", "")
                        if mount_point:
                            try:
                                subprocess.run(
                                    ["hdiutil", "detach", mount_point, "-force"],
                                    capture_output=True, timeout=10
                                )
                                ejected.append(os.path.basename(img_path))
                            except Exception:
                                pass
                if ejected:
                    logging.info(f"  Interplay: {len(ejected)} DMG(s) ausgeworfen: {ejected}")
                    time.sleep(1.0)  # Pro Tools braucht kurz um Volumes zu aktualisieren
        except Exception as e:
            logging.warning(f"  Interplay: DMG-Auswurf fehlgeschlagen: {e}")

        # Export-Spuren selektieren (nach Consolidate/Loudness)
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.3)

        # PT-Log Position merken – wird nach Export für Ergebnis-Erkennung genutzt
        pt_log_path  = _find_newest_pt_log()
        pt_log_start = os.path.getsize(pt_log_path) if pt_log_path and os.path.exists(pt_log_path) else 0
        logging.info(f"  Interplay: PT Log Snapshot: {pt_log_path} @ {pt_log_start:,} Bytes")

        prog["update"](0.67, t("prog_start_export"))
        logging.info("  Interplay: F13 senden...")
        _send_key(_VK_F13)

        # ── 1. Export Comment Fenster ─────────────────────────────────
        prog["update"](0.70, t("prog_wait_export_comment"))
        logging.info("  Interplay: Warte auf 'Export Comment'...")
        if not _wait_for_pt_window("Export Comment", timeout=10):
            logging.warning("  Interplay: 'Export Comment' nicht erschienen – Abbruch.")
            return False
        time.sleep(0.5)
        _send_key(_VK_RETURN)
        logging.info("  Interplay: Export Comment bestaetigt.")

        # ── 2. Export Options Fenster ─────────────────────────────────
        prog["update"](0.73, t("prog_export_workspace"))
        logging.info("  Interplay: Warte auf 'Export Options'...")
        if not _wait_for_pt_window("Export Options", timeout=10):
            logging.warning("  Interplay: 'Export Options' nicht erschienen – Abbruch.")
            return False
        time.sleep(1.0)

        # "Select Workspace" Radio Button per AppleScript klicken
        radio_script = '''
            tell application "System Events"
                tell process "Pro Tools"
                    set frontmost to true
                    tell window 1
                        set allRadios to every radio button
                        repeat with r in allRadios
                            set rName to name of r
                            if rName contains "Select Workspace" or rName contains "Workspace" then
                                click r
                                delay 0.5
                                return "OK: " & rName
                            end if
                        end repeat
                        return "RADIO NOT FOUND"
                    end tell
                end tell
            end tell
        '''
        rc, out, err = _run_applescript(radio_script)
        if "OK" in out:
            logging.info(f"  Interplay: Radio Button geklickt: {out}")
        else:
            logging.warning(f"  Interplay: Radio Button nicht gefunden: {out} {err}")

        time.sleep(0.5)

        # Popup-Position ermitteln fuer Mausklick (sucht das Popup neben dem Radio-Button)
        popup_script = '''
            tell application "System Events"
                tell process "Pro Tools"
                    tell window 1
                        set radioX to -1
                        set radioY to -1
                        set allRadios to every radio button
                        repeat with r in allRadios
                            if (name of r) contains "Workspace" then
                                set pos to position of r
                                set radioX to item 1 of pos
                                set radioY to item 2 of pos
                                exit repeat
                            end if
                        end repeat
                        
                        if radioY is not -1 then
                            set allPopups to every pop up button
                            repeat with p in allPopups
                                try
                                    set py to item 2 of (position of p)
                                    -- Gleiche Y-Hoehe (Toleranz 15 Pixel)
                                    if py - radioY < 15 and radioY - py < 15 then
                                        set pos to position of p
                                        set sz to size of p
                                        set cx to (item 1 of pos) + (item 1 of sz) / 2
                                        set cy to (item 2 of pos) + (item 2 of sz) / 2
                                        return "POPUP_OPEN:" & cx & "," & cy
                                    end if
                                end try
                            end repeat
                            
                            -- Fallback: Geometrisch rechts vom Radio-Button
                            set fallback_cx to radioX + 150
                            set fallback_cy to radioY + 8
                            return "POPUP_OPEN:" & fallback_cx & "," & fallback_cy
                        end if
                        return "NO_POPUP"
                    end tell
                end tell
            end tell
        '''
        rc, out, err = _run_applescript(popup_script)
        popup_x, popup_y = None, None
        if "POPUP_OPEN" in out:
            logging.info(f"  Interplay: Popup-Koordinaten gefunden: {out}")
            try:
                coords = out.split(":")[1].strip()
                popup_x = float(coords.split(",")[0])
                popup_y = float(coords.split(",")[1])
            except Exception:
                pass
        else:
            logging.warning(f"  Interplay: Kein Popup oder Radiobutton gefunden: {out} {err}")

        time.sleep(0.5)

        # Mausklick auf das geoeffnete Popup-Menue
        import Quartz
        if popup_x is not None and popup_y is not None:
            logging.info(f"  Interplay: Mausklick auf Popup ({popup_x:.0f}, {popup_y:.0f})...")
            point = Quartz.CGPointMake(popup_x, popup_y)
            click_down = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseDown, point, Quartz.kCGMouseButtonLeft
            )
            click_up = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseUp, point, Quartz.kCGMouseButtonLeft
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, click_down)
            time.sleep(0.05)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, click_up)
            time.sleep(0.5)
        else:
            logging.warning("  Interplay: Popup-Position unbekannt – ueberspringe Mausklick.")

        # Workspace per Pfeiltasten auswaehlen
        logging.info(f"  Interplay: {workspace_steps}x Pfeil-Hoch...")
        pid = _pt_pid()
        if pid:
            src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
            for _ in range(workspace_steps):
                ev_dn = Quartz.CGEventCreateKeyboardEvent(src, _VK_UP, True)
                ev_up = Quartz.CGEventCreateKeyboardEvent(src, _VK_UP, False)
                Quartz.CGEventPostToPid(pid, ev_dn)
                Quartz.CGEventPostToPid(pid, ev_up)
                time.sleep(0.05)
        time.sleep(0.3)
        _send_key(_VK_RETURN)
        logging.info("  Interplay: Workspace ausgewaehlt.")

        time.sleep(0.5)
        _send_key(_VK_RETURN)
        logging.info("  Interplay: Export Options bestaetigt.")

        # ── 3. Export-Ergebnis aus PT Log lesen, dann Bestätigung sofort senden ──
        prog["update"](0.80, t("prog_export_running"))
        logging.info("  Interplay: Warte auf Export-Ergebnis im PT Log...")
        export_ok, log_msg = _poll_pt_log_for_export(pt_log_path, pt_log_start, timeout=90)

        if export_ok is True:
            logging.info(f"  Interplay: Export ERFOLGREICH – {log_msg}")
            prog["update"](0.93, t("prog_export_success_confirm"))
        elif export_ok is False:
            logging.error(f"  Interplay: Export FEHLGESCHLAGEN – {log_msg}")
            prog["update"](0.93, t("prog_export_failed"))
        else:
            logging.warning(f"  Interplay: Export-Ergebnis unklar – {log_msg}")
            prog["update"](0.93, t("prog_export_done_ellipsis"))

        pt_dismiss_pos = os.path.getsize(pt_log_path) if pt_log_path and os.path.exists(pt_log_path) else 0
        _send_key(_VK_RETURN)
        logging.info("  Interplay: Bestaetigungsfenster bestaetigt.")
        _wait_for_pt_modal_dismissed(pt_log_path, pt_dismiss_pos)

        # ── 5. Sequence umbenennen – nur bei Erfolg oder unklarem Ergebnis ──
        if settings.get("interplay_rename_enabled", False):
            if export_ok is False:
                logging.warning("  Interplay: Umbenennung uebersprungen – Export war fehlerhaft.")
            else:
                prog["update"](0.97, t("prog_rename_seq"))
                logging.info("  Interplay: Starte Sequence-Umbenennung...")
                _rename_sequence_in_interplay(settings)

        prog["update"](1.0, t("prog_export_done"))
        time.sleep(0.8)
        logging.info("=== INTERPLAY EXPORT ENDE ===")
        return export_ok is not False   # False nur bei erkanntem Fehler

    except Exception as e:
        logging.error(f"Interplay Export Fehler: {e}", exc_info=True)
        _reset_if_rpc_error(e)  # toten gRPC-Channel verwerfen → kein Zombie
        if prog: prog["update"](1.0, f"{t('alert_error')}: {e}")
        return False
    finally:
        if prog: prog["close"]()
        _set_busy(False)


def _ia_send(action, pause=0.3):
    """Sendet eine Tastatur-Aktion an Interplay Access (System Events) und
    wartet kurz, bis die Java-UI sie verarbeitet hat."""
    _run_applescript(
        'tell application "System Events" to tell process "interplayAccess" '
        f'to {action}'
    )
    time.sleep(pause)


def _rename_sequence_in_interplay(settings):
    """Benennt die Sequenz in Interplay Access um.

    Das Rename-Feld wird nur EINMAL geöffnet:
      1. F2 → Feld öffnen, Cmd+A/Cmd+C → aktuellen Namen lesen
      2. Neuen Namen im Speicher nach den Einstellungen berechnen
      3. Auswahl direkt mit dem neuen Namen überschreiben, Enter

    AppleScript (System Events) analog zum Import-Workflow zur Vermeidung
    von Clipboard-Synchronisationsproblemen."""
    trim_start = max(0, int(settings.get("interplay_rename_trim_start", 0)))
    trim_end   = max(0, int(settings.get("interplay_rename_trim_end", 0)))
    prefix     = settings.get("interplay_rename_prefix", "")
    suffix     = settings.get("interplay_rename_suffix", "")

    if trim_start == 0 and trim_end == 0 and not prefix and not suffix:
        logging.info("  Rename: Keine Änderungen konfiguriert – überspringe.")
        return

    def _cancel():
        """Schließt das offene Rename-Feld ohne Änderung."""
        _ia_send(f'key code {_VK_ESC}')

    # Interplay Access aktivieren
    _activate_app("interplayAccess")
    try:
        _run_applescript(
            'tell application "System Events" to '
            'set frontmost of (first process whose name contains "Interplay") to true'
        )
    except Exception:
        pass
    time.sleep(0.8)

    # ── Schritt 1: F2 → Rename-Feld öffnen, Namen per Clipboard lesen ──────
    _ia_send(f'key code {_VK_F2}', pause=0.6)
    _ia_send('keystroke "a" using command down')
    _ia_send('keystroke "c" using command down', pause=0.5)

    try:
        current_name = subprocess.run(
            ["pbpaste"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
    except Exception as e:
        logging.warning(f"  Rename: pbpaste fehlgeschlagen: {e}")
        _cancel()
        return

    if not current_name:
        logging.warning("  Rename: Clipboard leer – Rename-Feld nicht erreichbar.")
        _cancel()
        return

    logging.info(f"  Rename: Aktueller Name: '{current_name}'")

    # ── Schritt 2: neuen Namen berechnen ──────────────────────────────────
    end_idx = max(trim_start, len(current_name) - trim_end if trim_end > 0 else len(current_name))
    new_name = prefix + current_name[trim_start:end_idx] + suffix

    if not new_name:
        logging.warning("  Rename: Berechneter Name ist leer – überspringe.")
        _cancel()
        return

    if new_name == current_name:
        logging.info("  Rename: Name unverändert – überspringe.")
        _cancel()
        return

    logging.info(f"  Rename: Neuer Name: '{new_name}'")

    # ── Schritt 3: Feld ist noch offen – Auswahl ersetzen und bestätigen ───
    # Cmd+A stellt sicher, dass der alte Name komplett selektiert ist;
    # keystroke ersetzt dann die Auswahl.
    _ia_send('keystroke "a" using command down')
    escaped_name = new_name.replace('\\', '\\\\').replace('"', '\\"')
    _ia_send(f'keystroke "{escaped_name}"', pause=0.5)
    _ia_send(f'key code {_VK_RETURN}')
    logging.info("  Rename: Umbenennung abgeschlossen.")


def run_export(export_tracks, video_track=None, settings=None):
    """
    Export-Workflow (Keyboard-basiert, ~5s):
      1. DoE und AD einblenden
      2. Video-Ende von V1 ermitteln
      3. Timeline setzen + Shift+Ö zum Ausdehnen auf alle Spuren
      4. Consolidate (alle Spuren auf einmal)
      5. Pre/Post-Material loeschen
      6. Export-Spuren am Spurenkopf markieren
    """
    with _export_lock:
        if state.export_running:
            logging.warning("Export laeuft bereits – ignoriert.")
            return
        state.export_running = True
    _set_busy(True)
    export_start_time = time.time()

    if settings is None:
        settings = load_settings()

    try:
        logging.info("=== EXPORT START ===")
        logging.info(f"  Export-Spuren: {export_tracks}")

        # Pro Tools in den Vordergrund (fuer Keyboard-Befehle)
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Pro Tools" to activate'],
                timeout=3, capture_output=True
            )
            time.sleep(0.3)
        except Exception:
            pass

        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfügbar – Abbruch.")
            return

        # ── Transport stoppen falls noch Aufnahme/Wiedergabe läuft ──
        _ensure_transport_stopped(engine)

        # ── 1. Versteckte Spuren einblenden ──────────────────────────
        logging.info(f"Schritt 1: Spuren einblenden: {export_tracks}...")
        try:
            engine.set_track_hidden_state(export_tracks, False)
            time.sleep(0.25)
        except Exception as e:
            logging.warning(f"  Spuren einblenden: {e}")

        # ── 2. Video-Ende ermitteln ──────────────────────────────────
        # Auto-detect oder Fallback auf Parameter/Settings
        video_track = video_track or _detect_video_track(engine, settings)
        logging.info(f"Schritt 2: Video-Ende ermitteln (Spur: '{video_track}')...")
        try:
            engine.select_all_clips_on_track(video_track)
            time.sleep(0.3)
            video_sel = engine.get_timeline_selection()
            video_end = video_sel[1]
            logging.info(f"  Video: {video_sel[0]} -> {video_end}")
        except Exception as e:
            logging.error(f"  Fehler Videospur '{video_track}': {e}")
            return

        if not video_end or video_end == "00:00:00:00.00":
            logging.error("  Video-Ende nicht ermittelt – Abbruch.")
            return

        in_time = settings.get("export_start_tc", "10:00:00:00") + ".00"

        # Pro Tools Selection State vorbereiten und absichern
        with _protools_selection_context(engine):
            # ── 3. Ueberhaenge pro Spur loeschen (vor dem Consolidate) ────
            logging.info("Schritt 3: Überhänge trimmen…")
            _trim_overhangs(engine, export_tracks, in_time, video_end)

            # ── 4. Timeline + Selection ausdehnen für Consolidate ────────
            logging.info(f"Schritt 4: Timeline {in_time} -> {video_end}")
            engine.select_tracks_by_name(export_tracks)
            time.sleep(0.25)
            engine.set_timeline_selection(in_time=in_time, out_time=video_end)
            time.sleep(0.25)
            engine.extend_selection_to_target_tracks(export_tracks)
            time.sleep(0.3)

            # ── 5. Consolidate (alle Spuren auf einmal) ──────────────────
            logging.info("Schritt 5: Consolidate...")
            engine.consolidate_clip()
            
            session_path = engine.session_path()
            session_dir = os.path.dirname(session_path)
            if os.path.basename(session_dir) == "Session File Backups":
                session_dir = os.path.dirname(session_dir)

            _wait_for_consolidate_window_gone(timeout=_consolidate_timeout(in_time, video_end))
            _wait_for_consolidated_files(session_dir, export_tracks, timeout=10)
            logging.info("  Consolidate OK")

            # ── 7. Loudness-Korrektur (EBU R128) ─────────────────────
            if settings.get("loudness_enabled", True):
                loud_tracks = settings.get("loudness_tracks", ["ST"])
                logging.info(f"Schritt 7: Loudness-Korrektur fuer {loud_tracks}...")
                try:
                    target_lufs = settings.get("target_lufs", -23.0)
                    max_tp = settings.get("max_truepeak", -3.0)
                    _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp)
                except Exception as e:
                    logging.error(f"  Normalisierung fehlgeschlagen: {e}")
            else:
                logging.info("Schritt 7: ST-Spur normalisieren uebersprungen (deaktiviert).")

        # -- 8a. WAV Export (optional) --
        if settings.get("wav_export_enabled", False):
            logging.info("Schritt 8a: WAV Export...")
            try:
                session_path = engine.session_path()
                s_dir = os.path.dirname(session_path)
                _do_wav_export(export_tracks, s_dir, min_mtime=export_start_time - 10.0,
                               export_dir=_resolve_export_dir(settings.get("wav_export_path"), s_dir))
            except Exception as e:
                logging.error(f"  WAV Export Fehler: {e}")
        else:
            logging.info("Schritt 8a: WAV Export uebersprungen (deaktiviert).")

        # -- 8b. AAF Export (optional) --
        if settings.get("aaf_export_enabled", False):
            logging.info("Schritt 8b: AAF Export...")
            try:
                session_path = engine.session_path()
                s_dir = os.path.dirname(session_path)
                s_name = os.path.splitext(os.path.basename(session_path))[0]
                _do_aaf_export(s_name, s_dir,
                               export_dir=_resolve_export_dir(settings.get("aaf_embedded_export_path"), s_dir))
            except Exception as e:
                logging.error(f"  AAF Export Fehler: {e}")
        else:
            logging.info("Schritt 8b: AAF Export uebersprungen (deaktiviert).")

        # -- 8c. Interplay Export (optional) --
        if settings.get("interplay_enabled", False):
            logging.info("Schritt 8c: Interplay Export...")
            ws_steps = settings.get("interplay_workspace_steps", 17)
            try:
                run_interplay_export(export_tracks, settings, workspace_steps=ws_steps)
            except Exception as e:
                logging.error(f"  Interplay Export Fehler: {e}")
        else:
            logging.info("Schritt 8c: Interplay Export uebersprungen (deaktiviert).")

        # -- 9. Abschluss: Spuren markieren, eingeblendet lassen --
        logging.info("Schritt 9: Export-Spuren markieren...")
        engine.set_timeline_selection(in_time=in_time, out_time=video_end)
        engine.select_tracks_by_name(export_tracks)

        logging.info("=== EXPORT ABGESCHLOSSEN ===")
        logging.info(f"  {len(export_tracks)} Spuren konsolidiert und bereinigt.")

    except Exception as e:
        logging.error(f"Export Fehler: {e}", exc_info=True)
        _reset_if_rpc_error(e)  # toten gRPC-Channel verwerfen → kein Zombie
    finally:
        with _export_lock:
            state.export_running = False
        _set_busy(False)


from contextlib import contextmanager

@contextmanager
def _protools_selection_context(engine):
    """
    Sichert die originalen Edit-Mode-Optionen und das Edit-Tool,
    setzt sie für die Export-Operationen auf Selector-Tool und Links aktiv,
    und stellt sie am Ende wieder her.
    """
    import ptsl.PTSL_pb2 as pt
    orig_tool = None
    orig_options = None
    
    # 1. Edit-Tool sichern und auf Selector setzen
    try:
        tool_res = engine.get_edit_tool()
        if tool_res is not None:
            if hasattr(tool_res, "current_setting"):
                orig_tool = tool_res.current_setting
            elif isinstance(tool_res, dict) and "current_setting" in tool_res:
                orig_tool = tool_res["current_setting"]
        logging.info("  PT: Selector-Tool aktivieren...")
        engine.set_edit_tool(pt.ETool_Selector)
    except Exception as e:
        logging.warning(f"  Selector-Tool konnte nicht gesetzt/gesichert werden: {e}")
        
    # 2. Edit-Mode-Optionen sichern und Links aktivieren
    try:
        _, opt_res = _ptsl_call(
            lambda: engine.client.run_command(pt.GetEditModeOptions, {}),
            label="GetEditModeOptions", timeout=5.0
        )
        if opt_res and "edit_mode_options" in opt_res:
            orig_options = dict(opt_res["edit_mode_options"])

        new_opts = dict(orig_options) if orig_options else {}
        new_opts["link_timeline_and_edit_selection"] = True
        new_opts["link_track_and_edit_selection"] = True

        logging.info("  PT: Link Timeline & Link Track aktivieren...")
        _ptsl_call(
            lambda: engine.client.run_command(pt.SetEditModeOptions, {"edit_mode_options": new_opts}),
            label="SetEditModeOptions", timeout=5.0
        )
    except Exception as e:
        logging.warning(f"  Edit-Mode-Optionen konnten nicht gesetzt/gesichert werden: {e}")

    try:
        yield
    finally:
        # 3. Originale Einstellungen wiederherstellen
        logging.info("  PT: Originale Edit-Optionen und Tools wiederherstellen...")
        try:
            if orig_options:
                _ptsl_call(
                    lambda: engine.client.run_command(pt.SetEditModeOptions, {"edit_mode_options": orig_options}),
                    label="RestoreEditModeOptions", timeout=5.0
                )
        except Exception as e:
            logging.warning(f"  Edit-Mode-Optionen Wiederherstellung fehlgeschlagen: {e}")
        try:
            if orig_tool is not None:
                engine.set_edit_tool(orig_tool)
        except Exception as e:
            logging.warning(f"  Edit-Tool Wiederherstellung fehlgeschlagen: {e}")


# -----------------------------------------------------------------------------
# WAV Export - Konsolidierte Dateien in Export-Ordner kopieren
# -----------------------------------------------------------------------------
def _trim_overhangs(engine, export_tracks, in_time, video_end):
    """Löscht Überhänge (Pre/Post-Material) außerhalb der Export-Range.

    Gebündelt: statt pro Spur einzeln wird die Pre-Region (vor Start-TC) bzw.
    Post-Region (nach Video-Ende) ÜBER ALLE Export-Spuren gleichzeitig
    selektiert und in EINEM Clear gelöscht. Das ersetzt 2×N Einzelschritte
    durch 2 Sammel-Schritte und ist deutlich schneller.
    """
    logging.info(f"  Ueberhaenge loeschen ({len(export_tracks)} Spuren, gebündelt)...")

    def _clear_range(in_t, out_t):
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.1)
        engine.set_timeline_selection(in_time=in_t, out_time=out_t)
        time.sleep(0.1)
        # Auswahl als Edit-Selection über alle Export-Spuren ausdehnen
        engine.extend_selection_to_target_tracks(export_tracks)
        time.sleep(0.1)
        try:
            engine.clear()
        except Exception as e:
            logging.debug(f"  Clear {in_t}->{out_t}: {e}")
        time.sleep(0.1)

    # Pre-Material (vor Start-TC) über ALLE Spuren auf einmal
    _clear_range("00:00:00:00.00", in_time)
    # Post-Material (nach Video-Ende) über ALLE Spuren auf einmal
    _clear_range(video_end, "23:59:59:24.00")

    logging.info("  Ueberhaenge geloescht")


# -----------------------------------------------------------------------------
def _do_wav_export(export_tracks, session_dir, min_mtime=None, export_dir=None):
    """Kopiert die konsolidierten WAV-Dateien in den Export-Ordner.
    Unterstützt sowohl Stereo-Interleaved als auch Split-Mono (.L.wav/.R.wav).
    Bei Split-Mono werden L+R zu einer Stereo-Interleaved-Datei zusammengeführt.
    export_dir: optionaler Zielordner (Standard: <session>/export).
    """
    audio_dir = os.path.join(session_dir, "Audio Files")
    if export_dir is None:
        export_dir = os.path.join(session_dir, "export")
    os.makedirs(export_dir, exist_ok=True)

    if not os.path.isdir(audio_dir):
        logging.error(f"  Audio-Ordner nicht gefunden: {audio_dir}")
        return

    def _matches_track(base_name, track_name):
        """Prüft ob ein Dateiname (ohne Extension) zu einem Track gehört."""
        return (base_name == track_name
                or base_name.startswith(track_name + "_")
                or base_name.startswith(track_name + ".")
                or base_name.startswith(track_name + "-"))

    copied = 0
    for track_name in export_tracks:
        # Alle WAV-Dateien für diesen Track sammeln – getrennt nach Typ
        interleaved = []   # Normale Stereo-Interleaved oder Mono WAVs
        split_L = []       # Split-Mono Left-Kanal (.L.wav)
        split_R = []       # Split-Mono Right-Kanal (.R.wav)

        for f in os.listdir(audio_dir):
            if not f.lower().endswith(".wav"):
                continue
            full = os.path.join(audio_dir, f)
            try:
                mtime = os.path.getmtime(full)
            except Exception:
                continue
            if min_mtime is not None and mtime < min_mtime:
                continue

            # Split-Mono erkennen: Dateiname endet auf .L.wav oder .R.wav
            if f.lower().endswith(".l.wav"):
                # Basis ohne .L.wav
                stem = f[:-len(".L.wav")]
                if _matches_track(stem, track_name):
                    split_L.append((os.path.getmtime(full), full, f, stem))
                continue
            elif f.lower().endswith(".r.wav"):
                stem = f[:-len(".R.wav")]
                if _matches_track(stem, track_name):
                    split_R.append((os.path.getmtime(full), full, f, stem))
                continue

            # Normale (interleaved) Datei
            base = os.path.splitext(f)[0]
            if _matches_track(base, track_name):
                interleaved.append((os.path.getmtime(full), full, f))

        dst = os.path.join(export_dir, f"{track_name}.wav")

        # Strategie 1: Interleaved-Datei vorhanden → direkt kopieren (bevorzugt)
        if interleaved:
            interleaved.sort(reverse=True)
            src = interleaved[0][1]
            shutil.copy2(src, dst)
            logging.info(f"  WAV: {interleaved[0][2]} -> {track_name}.wav")
            copied += 1

        # Strategie 2: Split-Mono L+R → zu Stereo zusammenführen
        elif split_L and split_R:
            split_L.sort(reverse=True)
            split_R.sort(reverse=True)
            l_file = split_L[0][1]
            r_file = split_R[0][1]
            try:
                import soundfile as sf
                import numpy as np
                data_l, rate_l = sf.read(l_file)
                data_r, rate_r = sf.read(r_file)
                # Auf gleiche Länge bringen (falls nötig)
                min_len = min(len(data_l), len(data_r))
                data_l = data_l[:min_len]
                data_r = data_r[:min_len]
                # Mono-Arrays zu Stereo zusammenführen
                if data_l.ndim == 1 and data_r.ndim == 1:
                    stereo = np.column_stack((data_l, data_r))
                else:
                    stereo = np.column_stack((data_l.flatten()[:min_len],
                                              data_r.flatten()[:min_len]))
                sf.write(dst, stereo, rate_l)
                logging.info(f"  WAV: Zusammenfuehren von {split_L[0][2]} und {split_R[0][2]} zu Stereo -> {track_name}.wav")
                copied += 1
            except Exception as e:
                logging.error(f"  WAV: Split-Mono Zusammenfuehrung fehlgeschlagen fuer '{track_name}': {e}")
        else:
            logging.warning(f"  WAV: Keine Datei fuer Spur '{track_name}' in Audio Files")

    logging.info(f"  WAV Export: {copied}/{len(export_tracks)} Dateien kopiert -> {export_dir}")


def run_wav_export_standalone(export_tracks, settings):
    """Standalone WAV Export – mit vorgelagertem Consolidate."""
    prog = None
    try:
        logging.info("=== WAV EXPORT (Standalone) START ===")
        _set_busy(True)
        export_start_time = time.time()
        prog = _show_progress_win(t("prog_title_wav"))
        prog["update"](0.03, t("prog_connect_pt"))
        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfuegbar.")
            return
        session_path = engine.session_path()
        session_dir = os.path.dirname(session_path)

        # Transport stoppen falls noch Aufnahme/Wiedergabe läuft
        _ensure_transport_stopped(engine)

        # Spuren einblenden
        prog["update"](0.06, t("prog_prep_tracks"))
        logging.info(f"  Spuren einblenden: {export_tracks}...")
        try:
            engine.set_track_hidden_state(export_tracks, False)
            time.sleep(0.25)
        except Exception as e:
            logging.warning(f"  Spuren einblenden: {e}")

        # Spuren selektieren
        prog["update"](0.08, t("prog_prep_tracks"))
        logging.info(f"  Spuren selektieren: {export_tracks}")
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.3)

        # Video-Ende ermitteln (wie im Haupt-Export)
        prog["update"](0.13, t("prog_get_video_end"))
        in_time = settings.get("export_start_tc", "10:00:00:00") + ".00"
        video_track = _detect_video_track(engine, settings)
        video_end = None
        try:
            engine.select_all_clips_on_track(video_track)
            time.sleep(0.3)
            video_sel = engine.get_timeline_selection()
            video_end = video_sel[1]
            logging.info(f"  Video: {video_sel[0]} -> {video_end}")
        except Exception as e:
            logging.error(f"  Fehler Videospur '{video_track}': {e}")

        if not video_end or video_end == "00:00:00:00.00":
            logging.error("  Video-Ende nicht ermittelt – Abbruch.")
            return

        # Pro Tools Selection State vorbereiten und absichern
        with _protools_selection_context(engine):
            # Überhänge pro Spur löschen (Pre/Post-Material abschneiden vor Consolidate)
            prog["update"](0.20, t("prog_trim_overhangs"))
            _trim_overhangs(engine, export_tracks, in_time, video_end)

            # Export-Spuren selektieren und Range für Consolidate setzen
            engine.select_tracks_by_name(export_tracks)
            time.sleep(0.25)
            logging.info(f"  Timeline: {in_time} -> {video_end}")
            engine.set_timeline_selection(in_time=in_time, out_time=video_end)
            time.sleep(0.25)
            # Auswahl auf die Export-Spuren ausdehnen
            engine.extend_selection_to_target_tracks(export_tracks)
            time.sleep(0.3)

            # Consolidate
            prog["update"](0.32, t("prog_consolidate"))
            logging.info("  Consolidate...")
            engine.consolidate_clip()
            if os.path.basename(session_dir) == "Session File Backups":
                session_dir = os.path.dirname(session_dir)
            _wait_for_consolidate_window_gone(timeout=_consolidate_timeout(in_time, video_end))
            _wait_for_consolidated_files(session_dir, export_tracks, timeout=10)
            logging.info("  Consolidate OK")

            # Loudness-Korrektur
            if settings.get("loudness_enabled", True):
                prog["update"](0.40, t("prog_loudness"))
                loud_tracks = settings.get("loudness_tracks", ["ST"])
                target_lufs = settings.get("target_lufs", -23.0)
                max_tp = settings.get("max_truepeak", -3.0)
                _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp)

        prog["update"](0.88, t("prog_copy_wav"))
        wav_dir = _resolve_export_dir(settings.get("wav_export_path"), session_dir)
        _do_wav_export(export_tracks, session_dir, min_mtime=export_start_time - 10.0,
                       export_dir=wav_dir)
        prog["update"](1.0, t("prog_wav_done"))
        time.sleep(0.8)
        logging.info("=== WAV EXPORT (Standalone) ENDE ===")
    except Exception as e:
        logging.error(f"WAV Export Fehler: {e}", exc_info=True)
        _reset_if_rpc_error(e)  # toten gRPC-Channel verwerfen → kein Zombie
        if prog: prog["update"](1.0, f"{t('alert_error')}: {e}")
    finally:
        if prog: prog["close"]()
        _set_busy(False)


# -----------------------------------------------------------------------------
# AAF Export - Pro Tools UI-Automation
# -----------------------------------------------------------------------------
_AAF_EXPORT_SCRIPT_TEMPLATE = '''
set exportPath to "{export_path}"

tell application "Pro Tools" to activate
delay 0.5

tell application "System Events"
    tell process "Pro Tools"
        set frontmost to true
        delay 0.3

        -- Datei > Export > Selected Tracks as New AAF/OMF...
        try
            click menu item "Selected Tracks as New AAF/OMF..." of menu of menu item "Export" of menu "File" of menu bar 1
        on error
            try
                click menu item "Selected Tracks as New AAF/OMF…" of menu of menu item "Export" of menu "File" of menu bar 1
            on error
                return "ERROR:Menu item not found"
            end try
        end try

        -- 1. Warte auf "Export to OMF/AAF" Fenster
        delay 1.5
        set exportWinFound to false
        repeat 30 times
            try
                set allWins to every window
                repeat with w in allWins
                    if name of w contains "Export to OMF" or name of w contains "Export to AAF" or name of w contains "OMF/AAF" then
                        set exportWinFound to true
                        exit repeat
                    end if
                end repeat
                if exportWinFound then exit repeat
            end try
            delay 0.5
        end repeat

        if not exportWinFound then
            return "ERROR:Export to OMF/AAF dialog not found"
        end if
        delay 0.5

        -- 2. Audio Media Options: Format-Dropdown gezielt auf "Embedded" setzen
        -- Group 3 = "Audio Media Options", Popup 3 darin = "Format"
        try
            set audioGroup to group 3 of window 1
            set formatPopup to pop up button 3 of audioGroup
            set currentFormat to value of formatPopup

            if currentFormat is not "Embedded" then
                click formatPopup
                delay 0.3
                key code 125
                delay 0.1
                key code 125
                delay 0.1
                key code 125
                delay 0.1
                key code 125
                delay 0.1
                key code 36 -- Enter
                delay 0.3
            end if
        on error errMsg
            try
                set allGroups to every group of window 1
                repeat with g in allGroups
                    try
                        if title of g contains "Audio" then
                            set formatPopup to pop up button 3 of g
                            click formatPopup
                            delay 0.3
                            key code 125
                            delay 0.1
                            key code 125
                            delay 0.1
                            key code 125
                            delay 0.1
                            key code 125
                            delay 0.1
                            key code 36
                            exit repeat
                        end if
                    end try
                end repeat
            end try
        end try


        delay 0.3

        -- 3. Dialog mit Enter bestaetigen
        key code 36
        delay 1.5

        -- 4. Warte auf "Publishing Options" Fenster
        set pubWinFound to false
        repeat 30 times
            try
                set allWins to every window
                repeat with w in allWins
                    if name of w contains "Publishing" then
                        set pubWinFound to true
                        exit repeat
                    end if
                end repeat
                if pubWinFound then exit repeat
            end try
            delay 0.5
        end repeat

        if not pubWinFound then
            return "ERROR:Publishing Options dialog not found"
        end if
        delay 0.5

        -- 5. Pfeiltaste rechts + "_Audio" tippen
        key code 124
        delay 0.1
        keystroke "_Audio"
        delay 0.3

        -- 6. Enter zum Bestaetigen
        key code 36
        delay 1.5

        -- 7. Warte auf "Save" Dialog
        set saveWinFound to false
        repeat 30 times
            try
                set allWins to every window
                repeat with w in allWins
                    if name of w contains "Save" or name of w contains "Speichern" then
                        set saveWinFound to true
                        exit repeat
                    end if
                end repeat
                if saveWinFound then exit repeat
            end try
            try
                if exists sheet 1 of window 1 then
                    set saveWinFound to true
                    exit repeat
                end if
            end try
            delay 0.5
        end repeat

        if not saveWinFound then
            return "ERROR:Save dialog not found"
        end if
        delay 0.5

        -- 8. Zum Export-Ordner navigieren via Cmd+Shift+G
        keystroke "g" using {{command down, shift down}}
        delay 0.8

        keystroke "a" using command down
        delay 0.1
        keystroke exportPath
        delay 0.3

        key code 36
        delay 0.8

        -- 9. Save mit Enter
        key code 36
        delay 1.5

        -- 9b. Replace-Dialog abfangen (falls Datei bereits existiert)
        -- Der Dialog hat einen LEEREN Fensternamen, daher direkt Yes/Ja Button suchen
        try
            if exists button "Yes" of window 1 then
                click button "Yes" of window 1
                delay 0.5
            end if
        end try
        try
            if exists button "Ja" of window 1 then
                click button "Ja" of window 1
                delay 0.5
            end if
        end try

        -- 9c. "Open"-Dialog "Please choose a folder for converted audio files"
        --     bestätigen (erscheint bei externen Medien NACH dem .aaf-Save;
        --     der angezeigte Ordner ist bereits der Export-Ordner).
        repeat 24 times
            set destDone to false
            try
                repeat with w in (every window)
                    if (name of w is "Open") then
                        try
                            click button "Open" of w
                        on error
                            key code 36
                        end try
                        set destDone to true
                        delay 0.8
                        exit repeat
                    end if
                end repeat
            end try
            if destDone then exit repeat
            delay 0.5
        end repeat

        -- 10. Warte bis Export abgeschlossen
        repeat 180 times
            set stillBusy to false
            
            -- Zuerst: Replace/Yes/Ja Dialoge sofort bestaetigen
            try
                if exists button "Yes" of window 1 then
                    click button "Yes" of window 1
                    delay 0.5
                end if
            end try
            try
                if exists button "Ja" of window 1 then
                    click button "Ja" of window 1
                    delay 0.5
                end if
            end try
            
            -- Dann: Pruefen ob noch Export-Fenster offen sind
            try
                set allWins to every window
                repeat with w in allWins
                    set wName to name of w
                    if wName contains "Export" or wName contains "Save" or wName contains "Publishing" or wName contains "Bouncing" or wName contains "Writing" then
                        set stillBusy to true
                        exit repeat
                    end if
                    -- Fenster ohne Namen mit Yes/No = Replace-Dialog
                    if wName is "" then
                        try
                            if exists button "Yes" of w then
                                click button "Yes" of w
                                set stillBusy to true
                                exit repeat
                            end if
                        end try
                        try
                            if exists button "Ja" of w then
                                click button "Ja" of w
                                set stillBusy to true
                                exit repeat
                            end if
                        end try
                    end if
                end repeat
            end try
            if not stillBusy then exit repeat
            delay 1.0
        end repeat

        delay 1.0

    end tell
end tell
return "OK"
'''


def _do_aaf_export(session_name, session_dir, export_dir=None):
    """Exportiert die selektierten Spuren als AAF mit Embedded Audio.
    export_dir: optionaler Zielordner (Standard: <session>/export)."""
    if export_dir is None:
        export_dir = os.path.join(session_dir, "export")
    os.makedirs(export_dir, exist_ok=True)

    script = _AAF_EXPORT_SCRIPT_TEMPLATE.format(
        export_path=export_dir.replace('"', '\\"')
    )

    logging.info(f"  AAF: Exportiere nach {export_dir}")
    rc, out, err = _run_applescript(script, timeout=300)
    if "ERROR" in out:
        logging.error(f"  AAF AppleScript Fehler: {out}")
    else:
        logging.info(f"  AAF Export abgeschlossen: {out}")




def run_aaf_export_standalone(export_tracks, settings):
    """Standalone AAF Export – mit vorgelagertem Consolidate."""
    prog = None
    try:
        logging.info("=== AAF EXPORT (Standalone) START ===")
        _set_busy(True)
        prog = _show_progress_win(t("prog_title_aaf"))
        prog["update"](0.03, t("prog_connect_pt"))
        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfuegbar.")
            return
        session_path = engine.session_path()
        session_dir = os.path.dirname(session_path)
        session_name = os.path.splitext(os.path.basename(session_path))[0]

        # Transport stoppen falls noch Aufnahme/Wiedergabe läuft
        _ensure_transport_stopped(engine)

        # Spuren einblenden
        prog["update"](0.06, t("prog_prep_tracks"))
        logging.info(f"  Spuren einblenden: {export_tracks}...")
        try:
            engine.set_track_hidden_state(export_tracks, False)
            time.sleep(0.25)
        except Exception as e:
            logging.warning(f"  Spuren einblenden: {e}")

        # Spuren selektieren
        prog["update"](0.08, t("prog_prep_tracks"))
        logging.info(f"  Spuren selektieren: {export_tracks}")
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.3)

        # Video-Ende ermitteln (wie im Haupt-Export)
        prog["update"](0.13, t("prog_get_video_end"))
        in_time = settings.get("export_start_tc", "10:00:00:00") + ".00"
        video_track = _detect_video_track(engine, settings)
        video_end = None
        try:
            engine.select_all_clips_on_track(video_track)
            time.sleep(0.3)
            video_sel = engine.get_timeline_selection()
            video_end = video_sel[1]
            logging.info(f"  Video: {video_sel[0]} -> {video_end}")
        except Exception as e:
            logging.error(f"  Fehler Videospur '{video_track}': {e}")

        if not video_end or video_end == "00:00:00:00.00":
            logging.error("  Video-Ende nicht ermittelt – Abbruch.")
            return

        # Pro Tools Selection State vorbereiten und absichern
        with _protools_selection_context(engine):
            # Überhänge pro Spur löschen (Pre/Post-Material abschneiden vor Consolidate)
            prog["update"](0.20, t("prog_trim_overhangs"))
            _trim_overhangs(engine, export_tracks, in_time, video_end)

            # Export-Spuren selektieren und Range für Consolidate setzen
            engine.select_tracks_by_name(export_tracks)
            time.sleep(0.25)
            logging.info(f"  Timeline: {in_time} -> {video_end}")
            engine.set_timeline_selection(in_time=in_time, out_time=video_end)
            time.sleep(0.25)
            # Auswahl auf die Export-Spuren ausdehnen
            engine.extend_selection_to_target_tracks(export_tracks)
            time.sleep(0.3)

            # Consolidate
            prog["update"](0.32, t("prog_consolidate"))
            logging.info("  Consolidate...")
            engine.consolidate_clip()
            if os.path.basename(session_dir) == "Session File Backups":
                session_dir = os.path.dirname(session_dir)
            _wait_for_consolidate_window_gone(timeout=_consolidate_timeout(in_time, video_end))
            _wait_for_consolidated_files(session_dir, export_tracks, timeout=10)
            logging.info("  Consolidate OK")

            # Loudness-Korrektur
            if settings.get("loudness_enabled", True):
                prog["update"](0.40, t("prog_loudness"))
                loud_tracks = settings.get("loudness_tracks", ["ST"])
                target_lufs = settings.get("target_lufs", -23.0)
                max_tp = settings.get("max_truepeak", -3.0)
                _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp)

        # Spuren erneut selektieren für AAF-Export
        prog["update"](0.85, t("prog_aaf_export"))
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.2)
        engine.set_timeline_selection(in_time=in_time, out_time=video_end)
        time.sleep(0.2)

        aaf_dir = _resolve_export_dir(settings.get("aaf_embedded_export_path"), session_dir)
        _do_aaf_export(session_name, session_dir, export_dir=aaf_dir)
        prog["update"](1.0, t("prog_aaf_done"))
        time.sleep(0.8)
        logging.info("=== AAF EXPORT (Standalone) ENDE ===")
    except Exception as e:
        logging.error(f"AAF Export Fehler: {e}", exc_info=True)
        _reset_if_rpc_error(e)  # toten gRPC-Channel verwerfen → kein Zombie
        if prog: prog["update"](1.0, f"{t('alert_error')}: {e}")
    finally:
        if prog: prog["close"]()
        _set_busy(False)


# ─────────────────────────────────────────────────────────────────────────────
# AAF Reference Export (externe WAV 24-bit, Consolidate-from-source mit Handle)
# ─────────────────────────────────────────────────────────────────────────────
# Wird im Pro-Tools-„Export to OMF/AAF"-Dialog gesetzt: Format = Copy/Consolidate
# from source media, Bit Depth 24, Handle = aaf_reference_handle_ms. Die exakte
# Dialog-Navigation ist versionsspezifisch und wird per Live-Diagnose kalibriert.
# Solange _AAF_REFERENCE_CALIBRATED False ist, bricht der Export sicher ab.
_AAF_REFERENCE_CALIBRATED = True
_AAF_REFERENCE_SCRIPT_TEMPLATE = '''
set exportPath to "{export_path}"

tell application "Pro Tools" to activate
delay 0.5

tell application "System Events"
    tell process "Pro Tools"
        set frontmost to true
        delay 0.3

        -- Datei > Export > Selected Tracks as New AAF/OMF...
        try
            click menu item "Selected Tracks as New AAF/OMF..." of menu of menu item "Export" of menu "File" of menu bar 1
        on error
            try
                click menu item "Selected Tracks as New AAF/OMF…" of menu of menu item "Export" of menu "File" of menu bar 1
            on error
                return "ERROR:Menu item not found"
            end try
        end try

        -- 1. Warte auf "Export to OMF/AAF" Fenster
        delay 1.5
        set exportWinFound to false
        repeat 30 times
            try
                set allWins to every window
                repeat with w in allWins
                    if name of w contains "Export to OMF" or name of w contains "Export to AAF" or name of w contains "OMF/AAF" then
                        set exportWinFound to true
                        exit repeat
                    end if
                end repeat
                if exportWinFound then exit repeat
            end try
            delay 0.5
        end repeat

        if not exportWinFound then
            return "ERROR:Export to OMF/AAF dialog not found"
        end if
        delay 0.5

        -- 2. Audio Media Options: Format=Consolidate from source media, {bit_depth}-bit, WAV, Handle {handle_ms}
        -- Index-basiert wie beim (bewährten) Embedded-Block: Popup 1 = Copy
        -- Option, Popup 2 = Bit Depth, Popup 3 = Format. Überschuss-sichere
        -- Pfeiltasten (Up = zum ersten, Down = zum letzten Eintrag).
        try
            set audioGroup to group 3 of window 1

            -- Copy Option (Popup 1) = "Consolidate from source media" (= erster Eintrag)
            set copPopup to pop up button 1 of audioGroup
            if (value of copPopup as text) is not "Consolidate from source media" then
                click copPopup
                delay 0.3
                key code 126
                delay 0.1
                key code 126
                delay 0.1
                key code 126
                delay 0.1
                key code 126
                delay 0.1
                key code 36
                delay 0.3
            end if

            -- Bit Depth (Popup 2) = "{bit_depth}" (24 = letzter Eintrag)
            set bitPopup to pop up button 2 of audioGroup
            if (value of bitPopup as text) is not "{bit_depth}" then
                click bitPopup
                delay 0.3
                key code 125
                delay 0.1
                key code 125
                delay 0.1
                key code 125
                delay 0.1
                key code 125
                delay 0.1
                key code 36
                delay 0.3
            end if

            -- Format (Popup 3) = "WAV" (= erster Eintrag)
            set fmtPopup to pop up button 3 of audioGroup
            if (value of fmtPopup as text) is not "WAV" then
                click fmtPopup
                delay 0.3
                key code 126
                delay 0.1
                key code 126
                delay 0.1
                key code 126
                delay 0.1
                key code 126
                delay 0.1
                key code 36
                delay 0.3
            end if

            -- Handle-Size-Textfeld auf {handle_ms} setzen
            try
                set hf to text field 1 of audioGroup
                set focused of hf to true
                delay 0.2
                keystroke "a" using command down
                delay 0.1
                keystroke "{handle_ms}"
                delay 0.1
                key code 48
                delay 0.2
            end try
        on error errMsg
            return "ERROR:Audio options failed: " & errMsg
        end try

        delay 0.3

        -- 3. Dialog mit Enter bestaetigen
        key code 36
        delay 1.5

        -- 4. Warte auf "Publishing Options" Fenster
        set pubWinFound to false
        repeat 30 times
            try
                set allWins to every window
                repeat with w in allWins
                    if name of w contains "Publishing" then
                        set pubWinFound to true
                        exit repeat
                    end if
                end repeat
                if pubWinFound then exit repeat
            end try
            delay 0.5
        end repeat

        if not pubWinFound then
            return "ERROR:Publishing Options dialog not found"
        end if
        delay 0.5

        -- 5. Pfeiltaste rechts + "_Audio" tippen
        key code 124
        delay 0.1
        keystroke "_Audio"
        delay 0.3

        -- 6. Enter zum Bestaetigen
        key code 36
        delay 1.5

        -- 7. Warte auf "Save" Dialog
        set saveWinFound to false
        repeat 30 times
            try
                set allWins to every window
                repeat with w in allWins
                    if name of w contains "Save" or name of w contains "Speichern" then
                        set saveWinFound to true
                        exit repeat
                    end if
                end repeat
                if saveWinFound then exit repeat
            end try
            try
                if exists sheet 1 of window 1 then
                    set saveWinFound to true
                    exit repeat
                end if
            end try
            delay 0.5
        end repeat

        if not saveWinFound then
            return "ERROR:Save dialog not found"
        end if
        delay 0.5

        -- 8. Zum Export-Ordner navigieren via Cmd+Shift+G
        keystroke "g" using {{command down, shift down}}
        delay 0.8

        keystroke "a" using command down
        delay 0.1
        keystroke exportPath
        delay 0.3

        key code 36
        delay 0.8

        -- 9. Save mit Enter
        key code 36
        delay 1.5

        -- 9b. Replace-Dialog abfangen
        try
            if exists button "Yes" of window 1 then
                click button "Yes" of window 1
                delay 0.5
            end if
        end try
        try
            if exists button "Ja" of window 1 then
                click button "Ja" of window 1
                delay 0.5
            end if
        end try

        -- 9c. "Open"-Dialog "Please choose a folder for converted audio files"
        --     bestätigen (erscheint bei externen Medien NACH dem .aaf-Save;
        --     der angezeigte Ordner ist bereits der Export-Ordner).
        repeat 24 times
            set destDone to false
            try
                repeat with w in (every window)
                    if (name of w is "Open") then
                        try
                            click button "Open" of w
                        on error
                            key code 36
                        end try
                        set destDone to true
                        delay 0.8
                        exit repeat
                    end if
                end repeat
            end try
            if destDone then exit repeat
            delay 0.5
        end repeat

        -- 10. Warte bis Export abgeschlossen
        repeat 180 times
            set stillBusy to false
            try
                if exists button "Yes" of window 1 then
                    click button "Yes" of window 1
                    delay 0.5
                end if
            end try
            try
                if exists button "Ja" of window 1 then
                    click button "Ja" of window 1
                    delay 0.5
                end if
            end try
            try
                set allWins to every window
                repeat with w in allWins
                    set wName to name of w
                    if wName contains "Export" or wName contains "Save" or wName contains "Publishing" or wName contains "Bouncing" or wName contains "Writing" then
                        set stillBusy to true
                        exit repeat
                    end if
                    if wName is "" then
                        try
                            if exists button "Yes" of w then
                                click button "Yes" of w
                                set stillBusy to true
                                exit repeat
                            end if
                        end try
                        try
                            if exists button "Ja" of w then
                                click button "Ja" of w
                                set stillBusy to true
                                exit repeat
                            end if
                        end try
                    end if
                end repeat
            end try
            if not stillBusy then exit repeat
            delay 1.0
        end repeat

        delay 1.0

    end tell
end tell
return "OK"
'''


def _do_aaf_reference_export(session_name, session_dir, export_dir, settings):
    """Exportiert die selektierten Spuren als AAF mit externen Referenz-WAVs
    (24-bit, Consolidate-from-source mit Handle). Pfad: export_dir."""
    os.makedirs(export_dir, exist_ok=True)
    if not _AAF_REFERENCE_CALIBRATED or not _AAF_REFERENCE_SCRIPT_TEMPLATE:
        msg = ("AAF-Reference-Dialogautomation noch nicht kalibriert – Export "
               "abgebrochen. (Bitte Diagnose-Schritt ausführen.)")
        logging.error("  " + msg)
        _show_error(t("alert_error"), msg)
        return
    handle_ms = settings.get("aaf_reference_handle_ms", 2000)
    bit_depth = settings.get("aaf_reference_bit_depth", 24)
    script = _AAF_REFERENCE_SCRIPT_TEMPLATE.format(
        export_path=export_dir.replace('"', '\\"'),
        handle_ms=handle_ms, bit_depth=bit_depth,
    )
    logging.info(f"  AAF-Reference: Exportiere nach {export_dir} "
                 f"(WAV {bit_depth}-bit, Handle {handle_ms}ms)")
    rc, out, err = _run_applescript(script, timeout=300)
    if "ERROR" in out:
        logging.error(f"  AAF-Reference AppleScript Fehler: {out}")
    else:
        logging.info(f"  AAF-Reference Export abgeschlossen: {out}")


def run_aaf_reference_export_standalone(export_tracks, settings):
    """Standalone AAF Reference Export.

    KEIN Pre-Consolidate / Trim / Loudness – Quellmaterial bleibt intakt, damit
    der AAF-Dialog Consolidate-from-source mit Handle erzeugen kann.
    """
    prog = None
    try:
        logging.info("=== AAF REFERENCE EXPORT (Standalone) START ===")
        _set_busy(True)
        prog = _show_progress_win(t("prog_title_aaf"))
        prog["update"](0.05, t("prog_connect_pt"))
        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfuegbar.")
            return
        session_path = engine.session_path()
        session_dir = os.path.dirname(session_path)
        session_name = os.path.splitext(os.path.basename(session_path))[0]

        # Transport stoppen falls noch Aufnahme/Wiedergabe läuft
        _ensure_transport_stopped(engine)

        # Spuren einblenden + selektieren
        prog["update"](0.10, t("prog_prep_tracks"))
        try:
            engine.set_track_hidden_state(export_tracks, False)
            time.sleep(0.25)
        except Exception as e:
            logging.warning(f"  Spuren einblenden: {e}")
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.3)

        # Video-Ende ermitteln
        prog["update"](0.20, t("prog_get_video_end"))
        in_time = settings.get("export_start_tc", "10:00:00:00") + ".00"
        video_track = _detect_video_track(engine, settings)
        video_end = None
        try:
            engine.select_all_clips_on_track(video_track)
            time.sleep(0.3)
            video_sel = engine.get_timeline_selection()
            video_end = video_sel[1]
            logging.info(f"  Video: {video_sel[0]} -> {video_end}")
        except Exception as e:
            logging.error(f"  Fehler Videospur '{video_track}': {e}")
        if not video_end or video_end == "00:00:00:00.00":
            logging.error("  Video-Ende nicht ermittelt – Abbruch.")
            return

        if os.path.basename(session_dir) == "Session File Backups":
            session_dir = os.path.dirname(session_dir)

        # Selektion setzen (KEIN Trim/Consolidate/Loudness – Handles brauchen Quellmaterial)
        prog["update"](0.50, t("prog_aaf_export"))
        engine.select_tracks_by_name(export_tracks)
        time.sleep(0.2)
        engine.set_timeline_selection(in_time=in_time, out_time=video_end)
        time.sleep(0.2)
        engine.extend_selection_to_target_tracks(export_tracks)
        time.sleep(0.3)

        export_dir = _resolve_export_dir(settings.get("aaf_reference_export_path"), session_dir)
        _do_aaf_reference_export(session_name, session_dir, export_dir, settings)
        prog["update"](1.0, t("prog_aaf_done"))
        time.sleep(0.8)
        logging.info("=== AAF REFERENCE EXPORT (Standalone) ENDE ===")
    except Exception as e:
        logging.error(f"AAF Reference Export Fehler: {e}", exc_info=True)
        _reset_if_rpc_error(e)  # toten gRPC-Channel verwerfen → kein Zombie
        if prog: prog["update"](1.0, f"{t('alert_error')}: {e}")
    finally:
        if prog: prog["close"]()
        _set_busy(False)

