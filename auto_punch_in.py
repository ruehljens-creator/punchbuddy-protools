#!/usr/bin/env python3
"""
PunchBuddy – Pro Tools Punch-In Automation
==========================================

Ablauf START (Hotkey):
  1. Ziel-Spuren in der Session finden
  2. Spuren Record-Enable AN
  3. Input Monitor AUS  (PTSL)
  4. Pre-Roll        EIN (CGEvent, non-blocking)
  5. Record starten  (PTSL toggle_play_state)

Ablauf STOP (Space oder Punch-Out):
  6. Warten bis Transport wirklich gestoppt (inkl. Post-Roll)
  7a. Pre-Roll   AUS  (CGEvent, non-blocking, Hintergrund-Thread)
  7b. Input Monitor EIN  (PTSL, parallel zu 7a)
"""

import ptsl
import time
import threading
import logging
import json
import os
import subprocess
import sys
import atexit
import gc
import select
import shutil
import glob
import copy
from punchbuddy import state
from punchbuddy.version import __version__ as PB_VERSION

# ─────────────────────────────────────────────────────────────────────────────
# Dock-Name + Prozessname SOFORT setzen (VOR import rumps / AppKit)
# rumps importiert intern AppKit und cached den Bundle-Namen.
# Deshalb muss CFBundleName VORHER überschrieben werden.
# ─────────────────────────────────────────────────────────────────────────────
try:
    from Foundation import NSBundle, NSProcessInfo
    _bundle = NSBundle.mainBundle()
    _info = _bundle.infoDictionary()
    if _info is not None:
        _info['CFBundleName'] = 'PunchBuddy'
        _info['CFBundleDisplayName'] = 'PunchBuddy'
    NSProcessInfo.processInfo().setProcessName_('PunchBuddy')
except Exception:
    pass

try:
    import ctypes, ctypes.util
    _libc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('c'))
    _libc.setprogname(b'PunchBuddy')
except Exception:
    pass

import rumps
import http.server
import socketserver
import socket

# ─────────────────────────────────────────────────────────────────────────────
# pyobjc (für AppleScript Pre-Roll-Erkennung)
# ─────────────────────────────────────────────────────────────────────────────
try:
    from Foundation import NSAppleScript, NSAutoreleasePool
    APPLESCRIPT_OK = True
except ImportError:
    NSAppleScript = NSAutoreleasePool = None
    APPLESCRIPT_OK = False

try:
    import AppKit
    import objc
    APPKIT_OK = True
except ImportError:
    APPKIT_OK = False

# ── Vocaster-Integration (optional – nur wenn libusb + Modul vorhanden) ───────
try:
    from vocaster_integration import VocasterController
    VOCASTER_OK = True
except Exception as _voc_err:
    VocasterController = None
    VOCASTER_OK = False
    logging.getLogger(__name__).debug(f"Vocaster-Integration nicht verfügbar: {_voc_err}")

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
# Logging/Pfade ausgelagert nach punchbuddy/log.py
from punchbuddy.log import _setup_log_dir, _LOG_DIR, LOG_PATH, _trim_log

# ─────────────────────────────────────────────────────────────────────────────
# Einstellungen
# ─────────────────────────────────────────────────────────────────────────────
# Einstellungen ausgelagert nach punchbuddy/config.py
from punchbuddy.config import (
    SETTINGS_PATH, DEFAULT_SETTINGS, _deep_merge, _PRESET_TEMPLATE,
    load_settings, save_settings, _migrate_settings,
    _OLD_SETTINGS_DIR, _NEW_SETTINGS_DIR,
)

# ─────────────────────────────────────────────────────────────────────────────
# Mehrsprachigkeit / Localization
# ─────────────────────────────────────────────────────────────────────────────
# i18n (Übersetzungen + Sprachauswahl) ausgelagert nach punchbuddy/i18n.py
from punchbuddy.i18n import (
    TRANSLATIONS, t, load_app_language, set_language, get_language,
)



def init_runtime():
    """Einmalige Laufzeit-Initialisierung mit Seiteneffekten (Sprache laden,
    Settings migrieren). Wird aus dem __main__-Block aufgerufen, NICHT beim
    Modul-Import – so bleibt `import auto_punch_in` nebenwirkungsfrei (testbar).
    Idempotent."""
    load_app_language(SETTINGS_PATH)
    _migrate_settings()



def webtrigger_token_ok(expected: str, supplied: str) -> bool:
    """True, wenn der Webtrigger-Request autorisiert ist. Leeres `expected`
    bedeutet kein Schutz (nur sinnvoll bei Bind auf 127.0.0.1). Sonst
    Konstantzeit-Vergleich gegen Timing-Angriffe."""
    expected = expected or ""
    if not expected:
        return True
    import hmac
    return hmac.compare_digest(supplied or "", expected)

# ─────────────────────────────────────────────────────────────────────────────
# CGEvent – Tastendruck direkt an Pro Tools (non-blocking)
# ─────────────────────────────────────────────────────────────────────────────
# Tastatur/PID-Infrastruktur ausgelagert nach punchbuddy/keys.py
from punchbuddy.keys import (
    _VK_K, _VK_F12, _VK_SPACE, _VK_R, _VK_A, _VK_F2, _VK_C, _VK_ESC,
    _VK_P, _VK_V, _VK_OE, _VK_F13, _VK_RETURN, _VK_TAB, _VK_DOWN,
    _pt_pid, _send_key, _app_pid, _send_key_to_app, _activate_app,
)

# ─────────────────────────────────────────────────────────────────────────────
# Pre-Roll Management (via PTSL – get/set_timeline_selection)
# ─────────────────────────────────────────────────────────────────────────────
# Pro Tools PTSL liefert pre_roll_enabled als Feld in GetTimelineSelection.
# Setzen erfolgt über set_timeline_selection(pre_roll_enabled=TB_True/TB_False).
# ─────────────────────────────────────────────────────────────────────────────

# PTSL-/Engine-Kern ausgelagert nach punchbuddy/engine.py
from punchbuddy.engine import (
    _get_preroll_status, _set_preroll_state, ensure_preroll_on, restore_preroll,
    _engine_lock, _GRPC_CALL_DEADLINE, _grpc_deadline_tls, _current_grpc_deadline,
    _install_grpc_deadline, _get_engine, _safe_close, _reset_engine,
    _invalidate_engine_health, _close_engine, _ptsl_lock, _ptsl_call,
    _invalidate_session_tracks, refresh_session_tracks, _get_cached_track_names,
    _show_error,
)

# Export-/Interplay-Funktionen ausgelagert nach punchbuddy/export.py
from punchbuddy.export import (
    run_interplay_import, run_interplay_export, run_export,
    run_wav_export_standalone, run_aaf_export_standalone,
    run_aaf_reference_export_standalone, _detect_video_track,
    _export_lock,
)

# Lautheits-Normalisierung ausgelagert nach punchbuddy/loudness.py
from punchbuddy.loudness import normalize_track, _run_loudness_with_progress

# Transport + geteilte AppKit-Helfer (von der UI/Webtrigger genutzt)
from punchbuddy.transport import (
    _stop_lock, run_punch_in, run_play_custom, run_play, run_stop,
    run_goto_start, run_move_audio, _set_busy, _send_shift_oe,
)
from punchbuddy.uikit import _show_progress_win
from punchbuddy.engine import current_engine, cached_track_count

# Globale Referenzliste für ObjC-Objekte des Einstellungsfensters
# (verhindert PyObjC-Dealloc-Crash; NIEMALS ObjC-Objekte als Instanz-Attribute
# speichern/überschreiben).
_config_refs = []  # wird in _open_unified_settings_window befüllt


def _close_existing_settings_window():
    """Schließt ein evtl. noch offenes Einstellungsfenster und gibt alle
    Referenzen frei. Gibt immer False zurück → der Aufrufer baut danach ein
    frisches Fenster.

    Früher wurde bei `isVisible()` das bestehende Fenster nur nach vorne
    geholt. Nach einem Speichern (modaler Dialog) lieferte `isVisible()` auf
    dem bereits geschlossenen Fenster jedoch fälschlich True, wodurch sich das
    Fenster kein zweites Mal öffnen ließ. Deshalb jetzt: immer schließen +
    Referenzen leeren + Neuaufbau erlauben."""
    global _config_refs
    if not _config_refs:
        return False  # kein Fenster offen
    try:
        win = _config_refs[0]
        win.close()
    except Exception:
        pass
    _config_refs = []
    return False

# ── Icon-Pfad ermitteln (global, wird für Dock, Fenster und Menüleiste verwendet) ─
_ICON_PNG_PATH = None

def _resolve_icon_path():
    """Ermittelt den Pfad zum PunchBuddy Icon (PNG)."""
    global _ICON_PNG_PATH
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "PunchBuddy.png"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Resources", "PunchBuddy.png"),
    ]
    for p in candidates:
        if os.path.exists(p):
            _ICON_PNG_PATH = p
            return p
    return None

def _load_ns_icon():
    """Lädt das PunchBuddy Icon als NSImage (für Dock und Fenster)."""
    if not APPKIT_OK:
        return None
    path = _ICON_PNG_PATH or _resolve_icon_path()
    if path and os.path.exists(path):
        try:
            icon = AppKit.NSImage.alloc().initWithContentsOfFile_(path)
            if icon:
                icon.setSize_(AppKit.NSMakeSize(128, 128))
                return icon
        except Exception as e:
            logging.warning(f"Icon laden fehlgeschlagen: {e}")
    return None

def _ensure_dock_icon():
    """Setzt PunchBuddy Icon UND Namen im Dock.
    
    Wenn ein Python-Script läuft, zeigt macOS im Dock standardmäßig
    'Python' als Name und das Python-Icon. Diese Funktion überschreibt
    beides über das NSBundle-InfoDictionary und die NSApplication API.
    """
    if not APPKIT_OK:
        return
    try:
        ns_app = AppKit.NSApplication.sharedApplication()

        # 1. Dock-Name ändern: CFBundleName im InfoDictionary überschreiben
        #    Damit zeigt macOS im Dock "PunchBuddy" statt "Python"
        bundle = AppKit.NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info:
            info['CFBundleName'] = 'PunchBuddy'

        # 2. Activation Policy auf Regular → App erscheint im Dock
        ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

        # 3. Dock-Icon setzen
        icon = _load_ns_icon()
        if icon:
            ns_app.setApplicationIconImage_(icon)

        logging.info("Dock: Name='PunchBuddy', Icon gesetzt.")
    except Exception as e:
        logging.warning(f"Dock-Icon/Name setzen fehlgeschlagen: {e}")

_resolve_icon_path()  # Beim Import einmal auflösen


def _get_network_interfaces():
    """Returns [(display_label, ip), ...] for all active network interfaces."""
    result = [("Localhost (127.0.0.1)", "127.0.0.1")]
    try:
        import subprocess as _sp
        out = _sp.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        current_iface = None
        for line in out.splitlines():
            if line and not line[0].isspace():
                current_iface = line.split(":")[0].strip()
            elif current_iface and "inet " in line and "inet6" not in line:
                parts = line.strip().split()
                try:
                    ip = parts[parts.index("inet") + 1]
                    if ip and ip != "127.0.0.1":
                        result.append((f"{current_iface}  ({ip})", ip))
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Zentrale Befehls-API
# ─────────────────────────────────────────────────────────────────────────────
# Standard-Pfad des netzwerkfreien Unix-Domain-Sockets.
DEFAULT_UNIX_SOCKET = "/tmp/punchbuddy.sock"

# Alias-Namen → kanonischer Befehl. Erlaubt kurze/alternative Schreibweisen
# und Rückwärtskompatibilität zu den bisherigen HTTP-Pfaden.
_COMMAND_ALIASES = {
    "trigger":  "record_a",
    "trigger2": "record_b",
    "a":        "record_a",
    "b":        "record_b",
    "rec_a":    "record_a",
    "rec_b":    "record_b",
    "start":    "goto_start",
    "goto":     "goto_start",
    "move":     "move_audio",
    "export_aaf_embedded": "export_aaf",
}


# ─────────────────────────────────────────────────────────────────────────────
# Menüleisten-App
# ─────────────────────────────────────────────────────────────────────────────
class PunchBuddyApp(rumps.App):
    def __init__(self):
        super().__init__("PunchBuddy", icon=_ICON_PNG_PATH, template=True,
                         quit_button=None)  # Custom Quit-Handler für Watchdog-Cleanup
        state.app_ref = self
        logging.info(f"PunchBuddy v{PB_VERSION} startet.")
        self.settings        = load_settings()

        # ── App Nap dauerhaft verhindern ─────────────────────────────────
        # Befund 2026-07-21 (Studio): macOS drosselt Hintergrund-Apps (App Nap)
        # – PunchBuddy ist als Menüleisten-App fast immer „hinten" (Pro Tools
        # vorne) und wurde dadurch bei Timer-/Netzwerkarbeit ausgebremst. Die
        # NSProcessInfo-Activity nimmt die App selbst davon aus – ohne dass am
        # Zielrechner `defaults write PunchBuddy NSAppSleepDisabled` nötig ist.
        # LatencyCritical + AllowingIdleSystemSleep: kein Nap, aber der Mac
        # darf weiterhin normal in den Ruhezustand.
        self._no_app_nap = None
        try:
            from Foundation import NSProcessInfo
            try:
                from Foundation import (NSActivityUserInitiatedAllowingIdleSystemSleep,
                                        NSActivityLatencyCritical)
                _opts = (NSActivityUserInitiatedAllowingIdleSystemSleep
                         | NSActivityLatencyCritical)
            except ImportError:  # Fallback: numerische Apple-Konstanten
                _opts = (0x00FFFFFF & ~(1 << 20)) | 0xFF00000000
            self._no_app_nap = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
                _opts, "PunchBuddy Echtzeit-Steuerung (Webtrigger/PTSL)")
            logging.info("App Nap deaktiviert (NSProcessInfo-Activity aktiv).")
        except Exception as e:
            logging.warning(f"App-Nap-Ausnahme konnte nicht gesetzt werden: {e}")

        # Partner-Apps der Steuerkette ebenfalls vom App Nap ausnehmen (idempotent).
        # Stream Deck (inkl. Chromium-Renderer des Web-Requests-Plugins) und Pro
        # Tools werden sonst gedrosselt, sobald sie im Hintergrund liegen -
        # Tastendruecke kamen dadurch erst Sekunden spaeter an (Befund 2026-07-21).
        # Wirkt fuer die jeweilige App ab ihrem naechsten Start.
        for _bid in ("com.elgato.StreamDeck", "com.avid.ProTools"):
            try:
                _cur = subprocess.run(["defaults", "read", _bid, "NSAppSleepDisabled"],
                                      capture_output=True, text=True, timeout=5).stdout.strip()
                if _cur != "1":
                    subprocess.run(["defaults", "write", _bid, "NSAppSleepDisabled",
                                    "-bool", "YES"], capture_output=True, timeout=5)
                    logging.info(f"App-Nap-Ausnahme gesetzt fuer {_bid} (wirkt ab dessen Neustart).")
            except Exception as e:
                logging.warning(f"App-Nap-Ausnahme fuer {_bid} fehlgeschlagen: {e}")

        # ── Trigger-Debounce (gegen Doppel-/Mehrfach-Trigger, z.B. Stream Deck
        #    der pro Tastendruck mehrere HTTP-Requests sendet, oder zu schnelles
        #    Hämmern). Gilt für ALLE triggerbaren Aktionen, Menü UND Webtrigger.
        #    Thread-sicher: Menü läuft im Main-Thread, HTTP in Worker-Threads.
        self._last_trigger = {}
        self._trigger_lock = threading.Lock()

        # ── Profil A (Standard) ──────────────────────────────────────────
        self.start_item      = rumps.MenuItem(t("record_a_start"),      callback=self._on_start)

        # ── Profil B (zweiter Trigger) ───────────────────────────────────
        self.start_b_item    = rumps.MenuItem(t("record_b_start"),      callback=self._on_start_b)

        # ── Play (Auto Monitor) ──────────────────────────────────────────
        self.play_item       = rumps.MenuItem(t("play_input"),      callback=self._on_start_play)

        # ── Play Custom ──────────────────────────────────────────────────
        self.play_custom_item = rumps.MenuItem(t("play_custom"), callback=self._on_start_play_custom)

        # ── Audio verschieben (Spur-Move) ────────────────────────────────
        self.move_audio_item = rumps.MenuItem(t("move_audio"), callback=self._on_move_audio)

        # ── Export ────────────────────────────────────────────────────────
        self.wav_export_item   = rumps.MenuItem(t("export_wav"),  callback=self._on_start_export_wav)
        self.aaf_export_item   = rumps.MenuItem(t("export_aaf_embedded"),  callback=self._on_start_export_aaf)
        self.aaf_ref_export_item = rumps.MenuItem(t("export_aaf_reference"), callback=self._on_start_export_aaf_reference)
        self.interplay_export_item = rumps.MenuItem(t("export_interplay"), callback=self._on_start_export_interplay)

        self.import_item       = rumps.MenuItem(t("interplay_import_start"),  callback=self._on_start_import)

        self.settings_item      = rumps.MenuItem(t("settings"),  callback=self._on_unified_settings)
        self.open_log_item      = rumps.MenuItem(t("open_log"), callback=self._on_open_log)
        self.refresh_tracks_item = rumps.MenuItem(t("refresh_tracks"), callback=self._on_refresh_tracks)

        # ── Menuestruktur ────────────────────────────────────────────────
        
        # 1. Haupt-Trigger (Top Level)
        self.menu = [
            self.start_item,
            self.start_b_item,
            self.play_item,
            self.play_custom_item,
            self.move_audio_item,
            rumps.separator,
        ]

        # Export-Untermenue
        self.export_menu = rumps.MenuItem(t("tab_export"))
        self.export_menu.add(self.wav_export_item)
        self.export_menu.add(self.aaf_export_item)
        self.export_menu.add(self.aaf_ref_export_item)
        self.export_menu.add(self.interplay_export_item)
        self.menu.add(self.export_menu)

        self.menu.add(self.import_item)
        self.menu.add(rumps.separator)

        # 2. Einstellungen (nur noch Fenster – URLs sind im Webtrigger-Tab)
        self.menu.add(rumps.separator)
        self.menu.add(self.settings_item)
        self.menu.add(self.refresh_tracks_item)
        self.menu.add(self.open_log_item)

        
        # 3. Hilfe & Info (Untermenü)
        self.help_menu = rumps.MenuItem(t("help_info"))
        
        self.manual_item = rumps.MenuItem(t("manual"), callback=self._on_help_manual)
        self.docs_item = rumps.MenuItem(t("tech_docs"), callback=self._on_help_docs)
        
        self.credits_title = rumps.MenuItem(t("developed_by"), callback=None)
        credits_names = rumps.MenuItem("Jens Rühl", callback=None)
        
        self.opensource_item = rumps.MenuItem(t("opensource"), callback=self._on_help_opensource)

        self.help_menu.add(self.manual_item)
        self.help_menu.add(self.docs_item)
        self.help_menu.add(rumps.separator)
        self.help_menu.add(self.opensource_item)
        self.help_menu.add(rumps.separator)
        self.help_menu.add(self.credits_title)
        self.help_menu.add(credits_names)

        self.menu.add(self.help_menu)
        self.menu.add(rumps.separator)
        
        self.quit_item = rumps.MenuItem(t("quit"), callback=self._on_quit)
        self.menu.add(self.quit_item)


        # Befehlstabelle einmalig im Main-Thread vorbauen, BEVOR Transporte
        # lauschen – so kann kein Server-Thread sie nebenläufig doppelt bauen.
        self._command_table()
        self._start_http()
        self._start_unix_socket()
        self._start_keepalive()
        self._start_vocaster()

        # ── Startup: Pre-Roll sicherstellen dass AUS ────────────────────────
        def _startup_preroll_check():
            time.sleep(2.0)  # Kurz warten bis PT vollständig bereit
            logging.info("=== Startup: Pre-Roll auf AUS stellen ===")
            restore_preroll()
            # Track-Cache beim Start befüllen – erspart PTSL-Call beim ersten Record
            eng = _get_engine()
            if eng is not None:
                refresh_session_tracks(eng)

        threading.Thread(target=_startup_preroll_check, daemon=True, name="StartupPreRoll").start()

        # ── Tooltip für Menüleisten-Icon setzen (nach App-Start) ───────────
        self._tooltip_timer = rumps.Timer(self._set_tooltip, 1)
        self._tooltip_timer.start()

    # ── Internes ──────────────────────────────────────────────────────────

    def _set_tooltip(self, timer):
        """Setzt Tooltip und Dock-Icon nach App-Start (NSApp existiert jetzt)."""
        timer.stop()
        try:
            # ── Tooltip für Menüleisten-Icon ────────────────────────────────
            if hasattr(self, '_nsapp') and self._nsapp:
                nsstatusitem = getattr(self._nsapp, 'nsstatusitem', None)
                if nsstatusitem:
                    nsstatusitem.button().setToolTip_(f"PunchBuddy v{PB_VERSION}")
                    logging.info(f"Menüleisten-Tooltip gesetzt: PunchBuddy v{PB_VERSION}")
        except Exception as e:
            logging.debug(f"Tooltip setzen fehlgeschlagen: {e}")

        # ── Dock-Icon sofort beim Start anzeigen ─────────────────────────
        _ensure_dock_icon()

    def load_preset_by_index(self, idx):
        presets = self.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if not (0 <= idx < len(presets)):
            logging.error(f"Preset-Index {idx} ungültig (0-{len(presets)-1})")
            return False

        p = presets[idx]

        # Spurenzuweisungen anwenden
        self.settings["tracks"] = p.get("rec_a", [])
        self.settings["monitor_tracks"] = p.get("mon_a", [])
        self.settings["tracks_b"] = p.get("rec_b", [])
        self.settings["monitor_tracks_b"] = p.get("mon_b", [])
        self.settings["export_tracks"] = p.get("export_tracks", p.get("export", []))
        self.settings["loudness_tracks"] = p.get("loudness_tracks", [])
        self.settings["play_monitor_tracks"] = p.get("play_monitor_tracks", [])

        # Kontrollen-Keys anwenden
        bool_keys = ["wav_export_enabled", "aaf_export_enabled", "interplay_enabled",
                     "loudness_enabled", "interplay_rename_enabled", "import_close_session"]
        for key in bool_keys:
            if key in p:
                self.settings[key] = p[key]

        str_keys = ["export_start_tc", "video_track", "interplay_workspace",
                    "export_error_keywords", "export_success_keywords",
                    "interplay_rename_prefix", "interplay_rename_suffix"]
        for key in str_keys:
            if key in p:
                self.settings[key] = p[key]

        int_keys = ["extend_count", "interplay_workspace_steps",
                    "interplay_rename_trim_start", "interplay_rename_trim_end", "http_port"]
        for key in int_keys:
            if key in p:
                self.settings[key] = p[key]

        float_keys = ["target_lufs", "max_truepeak"]
        for key in float_keys:
            if key in p:
                self.settings[key] = p[key]

        if "http_bind_host" in p:
            self.settings["http_bind_host"] = p["http_bind_host"]

        save_settings(self.settings)
        logging.info(f"Preset {idx} ('{p.get('name')}') erfolgreich geladen und gespeichert.")
        return True

    def _start_http(self):
        app_ref = self

        class _ThreadingHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
            daemon_threads = True
            allow_reuse_address = True

        class Handler(http.server.BaseHTTPRequestHandler):
            # Socket-Timeout gegen langsame/hängende Clients (Slowloris): die
            # Verbindung wird nach 30 s abgebrochen, statt einen Thread ewig zu
            # blockieren (StreamRequestHandler wertet diese Klassenvariable aus).
            timeout = 30

            # HTTP-Pfad → Befehlsname der zentralen API. Alle hier gelisteten
            # Endpunkte verhalten sich byte-genau wie bisher (200 OK + "OK",
            # Aktion wird gequeued), laufen aber jetzt über command_dispatch().
            _PATH_CMD = {
                "/trigger": "record_a", "/trigger2": "record_b",
                "/import": "import",
                "/export_wav": "export_wav",
                "/export_aaf": "export_aaf", "/export_aaf_embedded": "export_aaf",
                "/export_aaf_reference": "export_aaf_reference",
                "/export_interplay": "export_interplay",
                "/play": "play", "/play_custom": "play_custom",
                "/start": "goto_start", "/move_audio": "move_audio",
            }
            _VOC_CMD = {
                "/vocaster/autogain/host":  "vocaster_autogain_host",
                "/vocaster/autogain/guest": "vocaster_autogain_guest",
                "/vocaster/phantom/on":     "vocaster_phantom_on",
                "/vocaster/phantom/off":    "vocaster_phantom_off",
            }

            def _reply(self, code, text):
                self.send_response(code)
                self.send_header("Content-type", "text/plain; charset=utf-8")
                self.end_headers()
                try:
                    self.wfile.write(text.encode("utf-8"))
                except Exception:
                    pass

            def _handle_vocaster(self, app_ref, path):
                """Vocaster-Webtrigger → zentrale Befehls-API."""
                cmd = self._VOC_CMD.get(path)
                if cmd is None:
                    self._reply(404, "not found"); return
                ok, msg = app_ref.command_dispatch(cmd)
                if not ok and "no vocaster" in msg:
                    self._reply(503, "Kein Vocaster angeschlossen"); return
                self._reply(200 if ok else 500, "OK" if ok else msg)

            def _authorized(self, query):
                """Prüft das Webtrigger-Token, falls eines konfiguriert ist.
                Token via ?token=… oder X-Auth-Token-Header."""
                expected = app_ref.settings.get("webtrigger_token", "") or ""
                supplied = (query.get("token", [""])[0]
                            or self.headers.get("X-Auth-Token", ""))
                return webtrigger_token_ok(expected, supplied)

            def do_GET(self):
                from urllib.parse import urlparse, parse_qs
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                if not self._authorized(query):
                    self._reply(401, "Unauthorized")
                    return

                if path in self._PATH_CMD:
                    # Sofort 200 OK; Aktion läuft gequeued (nicht blockierend).
                    cmd = self._PATH_CMD[path]
                    self._reply(200, "OK")
                    threading.Thread(target=lambda: app_ref.command_dispatch(cmd),
                                     daemon=True).start()
                elif path == "/commands":
                    _, msg = app_ref.command_dispatch("list")
                    self._reply(200, msg)
                elif path == "/command" or path.startswith("/command/"):
                    # /command/<name>[/<arg>]  ODER  /command?c=<name>&arg=<x>
                    name, cargs = "", []
                    if path.startswith("/command/"):
                        parts = [p for p in path.split("/") if p][1:]  # nach 'command'
                        if parts:
                            name, cargs = parts[0], parts[1:]
                    if not name:
                        name = query.get("c", [""])[0] or query.get("cmd", [""])[0]
                    if not cargs:
                        cargs = query.get("arg", [])
                    if not name:
                        self._reply(400, "missing command name"); return
                    ok, msg = app_ref.command_dispatch(name, cargs)
                    self._reply(200 if ok else 400, msg)
                elif path.startswith("/vocaster/"):
                    self._handle_vocaster(app_ref, path)
                elif path.startswith("/preset/"):
                    try:
                        preset_num = int(path.split("/preset/")[1])
                    except Exception:
                        # Keine Roh-Exception/User-Eingabe zurückspiegeln.
                        self._reply(400, "Ungueltiger Preset Index"); return
                    ok, msg = app_ref.command_dispatch("preset", [preset_num])
                    if ok and "loaded" in msg:
                        self._reply(200, f"Preset {preset_num} geladen")
                    elif ok and "debounced" in msg:
                        self._reply(200, f"Preset {preset_num} entprellt")
                    else:
                        self._reply(400, "Ungueltiger Preset Index")
                else:
                    self._reply(404, "not found")

            def log_message(self, fmt, *args):
                pass  # kein HTTP-Logging in Konsole

        try:
            port = self.settings.get("http_port", 8899)
            bind_host = self.settings.get("http_bind_host", "127.0.0.1")
            actual_bind = "127.0.0.1" if bind_host == "127.0.0.1" else "0.0.0.0"

            # Sicherheit: Bei Bind auf alle Interfaces (LAN erreichbar) ist ein
            # Token Pflicht. Fehlt es, wird einmalig eines erzeugt und gespeichert,
            # damit der Server nicht ungeschützt im Netz steht.
            if actual_bind == "0.0.0.0" and not self.settings.get("webtrigger_token"):
                import secrets
                self.settings["webtrigger_token"] = secrets.token_urlsafe(16)
                save_settings(self.settings)
                logging.warning(
                    "Webtrigger ist im LAN erreichbar (0.0.0.0) – automatisch ein "
                    "Token erzeugt. Stream-Deck-URLs müssen ?token=… anhängen.")
            _tok = self.settings.get("webtrigger_token", "")
            if _tok:
                logging.info(f"Webtrigger-Token aktiv (an URLs anhängen: ?token={_tok})")

            self.httpd = None
            for attempt in range(4):
                try:
                    self.httpd = _ThreadingHTTPServer((actual_bind, port), Handler)
                    break
                except OSError as e:
                    if attempt < 3:
                        logging.warning(f"Port {port} belegt (Versuch {attempt+1}/4), warte 0.5s...")
                        time.sleep(0.5)
                    else:
                        logging.warning(f"Port {port} dauerhaft belegt – weiche auf freien Port aus (wird NICHT gespeichert)...")
                        self.httpd = _ThreadingHTTPServer((actual_bind, 0), Handler)
                        port = self.httpd.server_address[1]
            self._http_port = port
            self._http_bind_host_display = bind_host
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
            atexit.register(self.httpd.shutdown)
            logging.info(f"Stream Deck HTTP-Server lauscht auf {actual_bind}:{port} (Anzeige: {bind_host})")
            logging.info(f"  /trigger  → Profil A")
            logging.info(f"  /trigger2 → Profil B")
            logging.info(f"  /export   → Export (komplett)")
            logging.info(f"  /import   → Interplay Import")
            logging.info(f"  /export_wav → WAV Export")
            logging.info(f"  /export_aaf / /export_aaf_embedded → AAF Embedded")
            logging.info(f"  /export_aaf_reference → AAF Reference")
            logging.info(f"  /export_interplay → Interplay Export")
            logging.info(f"  /play       → Play/Stop (Toggle)")
            logging.info(f"  /play_custom → Play Custom (Mute KH2/Unmute ST Abh)")
            logging.info(f"  /start       → Cursor auf Start-Timecode")
            logging.info(f"  /move_audio  → Audio von Quell- auf Ziel-Spuren verschieben")
            logging.info(f"  /preset/{{1-8}} → Preset 1-8 laden")
        except Exception as e:
            self._http_port = 8899  # Fallback für URL-Anzeige
            self._http_bind_host_display = self.settings.get("http_bind_host", "127.0.0.1")
            logging.error(f"HTTP-Server Start fehlgeschlagen: {e}")

    def _restart_http(self):
        if hasattr(self, 'httpd') and self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
        self._start_http()

    # ── Unix-Domain-Socket-Steuerung (netzwerkfrei) ─────────────────────────
    #    Bedient dieselbe zentrale Befehls-API wie der HTTP-Webtrigger, aber
    #    komplett OHNE IP-Stack (kein TCP, kein Loopback) – damit für
    #    Netzwerk-Filter (z. B. Microsoft Defender) prinzipiell unsichtbar.
    #    Protokoll (zeilenbasiert, eine Verbindung = ein Befehl):
    #        Client schreibt:  "<befehl> [arg ...]\n"   z. B. "play\n", "preset 3\n"
    #        Server antwortet: "OK <msg>\n"  oder  "ERR <msg>\n"  und schliesst.
    #    Test:  printf 'play' | nc -U /tmp/punchbuddy.sock
    @objc.python_method
    def _start_unix_socket(self):
        if not self.settings.get("unix_socket_enabled", True):
            logging.info("Unix-Socket-Steuerung deaktiviert (Einstellung unix_socket_enabled=false).")
            return
        sock_path = os.path.expanduser(
            self.settings.get("unix_socket_path", DEFAULT_UNIX_SOCKET) or DEFAULT_UNIX_SOCKET)
        app_ref = self
        srv = None
        try:
            # Stale Socket-Datei eines vorigen Laufs entfernen.
            if os.path.exists(sock_path):
                os.unlink(sock_path)
            parent = os.path.dirname(sock_path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            os.chmod(sock_path, 0o600)   # nur der angemeldete Benutzer darf verbinden
            srv.listen(16)
        except Exception as e:
            logging.error(f"Unix-Socket konnte nicht gestartet werden ({sock_path}): {e}")
            if srv is not None:                       # FD nicht lecken, wenn chmod/listen scheitert
                try:
                    srv.close()
                except OSError:
                    pass
            return

        self._unix_sock = srv
        self._unix_sock_path = sock_path

        def _handle_conn(conn):
            try:
                conn.settimeout(5.0)
                data = b""
                while b"\n" not in data and len(data) < 4096:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                if b"\n" not in data and len(data) >= 4096:
                    # Überlange Zeile ohne Zeilenende → ablehnen, nicht still abschneiden.
                    try:
                        conn.sendall(b"ERR line too long\n")
                    except OSError:
                        pass
                    return
                line = data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
                ok, msg = app_ref._dispatch_socket_line(line)
                try:
                    conn.sendall((("OK " if ok else "ERR ") + msg + "\n").encode("utf-8"))
                except OSError:
                    pass
            except Exception as e:
                logging.debug(f"Unix-Socket-Verbindungsfehler: {e}")
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

        def _serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break  # Socket wurde geschlossen (Quit) → Schleife beenden
                threading.Thread(target=_handle_conn, args=(conn,),
                                 daemon=True, name="UnixSockConn").start()

        threading.Thread(target=_serve, daemon=True, name="UnixSocketServer").start()
        atexit.register(self._close_unix_socket)
        logging.info(f"Unix-Socket-Steuerung aktiv: {sock_path}")
        logging.info(f"  Test:  printf 'play' | nc -U {sock_path}")
        logging.info(f"  Befehle:  printf 'list' | nc -U {sock_path}")

    @objc.python_method
    def _close_unix_socket(self):
        srv = getattr(self, "_unix_sock", None)
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
            self._unix_sock = None
        p = getattr(self, "_unix_sock_path", None)
        if p:
            try:
                if os.path.exists(p):
                    os.unlink(p)
            except OSError:
                pass
            self._unix_sock_path = None

    @objc.python_method
    def _dispatch_socket_line(self, line):
        """Parst eine Socket-Zeile ('<befehl> [arg ...]') und ruft die zentrale
        Befehls-API. Der lokale Socket ist über Datei-Rechte 0600 bereits auf
        den angemeldeten Benutzer beschränkt – kein zusätzliches Token nötig.
        Ein optionales 'token=…'-Argument wird toleriert (und ignoriert)."""
        if not line:
            return (False, "empty")
        parts = line.split()
        cmd = parts[0]
        args = [a for a in parts[1:] if not a.startswith("token=")]
        return self.command_dispatch(cmd, args)

    # ── Vocaster-Integration ────────────────────────────────────────────────
    def _start_vocaster(self):
        """
        Initialisiert die Vocaster-Steuerung, falls verfügbar und ein Gerät
        angeschlossen ist. Richtet den Main-Thread-Driver-Timer ein und schaltet
        – falls in den Einstellungen aktiviert – beim Start die 48V ein.
        """
        self.vocaster = None
        self._vocaster_info = None
        if not (VOCASTER_OK and APPKIT_OK):
            return
        try:
            info = VocasterController.detect()
        except Exception as e:
            logging.debug(f"Vocaster-Erkennung übersprungen: {e}")
            info = None
        if not info:
            logging.info("Kein Vocaster erkannt – Vocaster-Funktionen inaktiv.")
            return

        self._vocaster_info = info
        self.vocaster = VocasterController(notify=self._vocaster_notify)
        logging.info(f"Vocaster erkannt: {info.get('name')} "
                     f"({info.get('channels')} Eingang/Eingänge)")

        # Main-Thread-Driver-Timer (treibt Autogain-Fenster + Status-Polling)
        self._vocaster_timer = rumps.Timer(lambda _t: self.vocaster.drive(), 0.4)
        self._vocaster_timer.start()

        # Menü für manuelle Steuerung einfügen
        try:
            self._build_vocaster_menu(info.get("channels", 1))
        except Exception as e:
            logging.debug(f"Vocaster-Menü nicht erstellt: {e}")

        # 48V beim Start einschalten (falls aktiviert)
        if self.settings.get("vocaster_phantom_on_start", False):
            logging.info("Vocaster: 48V beim Start aktiviert – schalte ein.")
            self.vocaster.set_phantom(True, announce=True)

        # Gespeichertes Routing beim Start anwenden (falls aktiviert)
        if (self.settings.get("vocaster_apply_routing_on_start", False)
                and self.vocaster.has_saved_routing()):
            logging.info("Vocaster: gespeichertes Routing wird angewendet.")
            def _do_apply():
                ok, msg = self.vocaster.apply_saved_routing()
                if ok:
                    rumps.notification("PunchBuddy – Vocaster", "Routing angewendet", msg)
                else:
                    rumps.notification("PunchBuddy – Vocaster", "Routing-Fehler", msg)
            threading.Thread(target=_do_apply, daemon=True).start()

    def _build_vocaster_menu(self, channels):
        """Fügt ein Vocaster-Untermenü (manuelle Steuerung) vor 'Hilfe & Info' ein."""
        voc_menu = rumps.MenuItem("Vocaster")
        voc_menu.add(rumps.MenuItem(t("vocaster_menu_autogain_host"),
                                    callback=self._on_vocaster_autogain_host))
        if channels >= 2:
            voc_menu.add(rumps.MenuItem(t("vocaster_menu_autogain_guest"),
                                        callback=self._on_vocaster_autogain_guest))
        voc_menu.add(rumps.separator)
        voc_menu.add(rumps.MenuItem(t("vocaster_menu_phantom_on"),
                                    callback=self._on_vocaster_phantom_on))
        voc_menu.add(rumps.MenuItem(t("vocaster_menu_phantom_off"),
                                    callback=self._on_vocaster_phantom_off))
        voc_menu.add(rumps.separator)
        voc_menu.add(rumps.MenuItem(t("vocaster_menu_save_routing"),
                                    callback=self._on_vocaster_save_routing))
        self._vocaster_menu = voc_menu
        try:
            self.menu.insert_before(t("help_info"), voc_menu)
            self.menu.insert_before(t("help_info"), rumps.separator)
        except Exception:
            self.menu.add(voc_menu)

    def _on_vocaster_autogain_host(self, _):
        if self.vocaster:
            self.vocaster.request_autogain(0, "Host")

    def _on_vocaster_autogain_guest(self, _):
        if self.vocaster:
            self.vocaster.request_autogain(1, "Guest")

    def _on_vocaster_phantom_on(self, _):
        if self.vocaster:
            self.vocaster.set_phantom(True)

    def _on_vocaster_phantom_off(self, _):
        if self.vocaster:
            self.vocaster.set_phantom(False)

    def _on_vocaster_save_routing(self, _):
        """Liest das aktuelle MUX-Routing vom Gerät und speichert es."""
        if not self.vocaster:
            return
        def work():
            ok, msg = self.vocaster.capture_routing_now()
            title = "PunchBuddy – Vocaster"
            if ok:
                rumps.notification(title, t("msg_routing_captured"), msg)
                # UI im Settings-Fenster ggf. neu rendern bei nächstem Öffnen
            else:
                rumps.notification(title, "Fehler", msg)
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _vocaster_notify(title, subtitle, message):
        try:
            rumps.notification(title, subtitle, message)
        except Exception:
            pass

    def _start_keepalive(self):
        """Startet einen Hintergrund-Thread der alle 120s transport_state() abfragt.
        Hält die PTSL-Verbindung warm und verhindert den NEXIS-Cold-Start-Delay."""
        _KEEPALIVE_INTERVAL = 120.0

        def _loop():
            while True:
                time.sleep(_KEEPALIVE_INTERVAL)
                # Nicht pingen während Aufnahme oder Import läuft
                if state.running or state.import_running:
                    continue
                eng = current_engine()
                if eng is None:
                    continue
                # Über _ptsl_call: serialisiert via _ptsl_lock (kein Race mit
                # echten Befehlen) und mit gRPC-Deadline. Ein Fehler verwirft
                # die Engine, der nächste Trigger verbindet frisch neu.
                ok, _ = _ptsl_call(eng.transport_state, label="KeepAlive", timeout=3.0)
                if ok:
                    logging.debug("KeepAlive: transport_state() OK")
                else:
                    logging.debug("KeepAlive: transport_state() fehlgeschlagen")

        threading.Thread(target=_loop, daemon=True, name="PTSLKeepAlive").start()
        logging.info("PTSL Keep-Alive gestartet (Intervall: 120s)")

    @objc.python_method
    def _debounce_ok(self, key, interval=0.4):
        """True, wenn der Trigger ausgeführt werden darf; False, wenn er als
        Doppel-/Mehrfach-Trigger innerhalb von `interval` Sekunden verworfen
        wird. Fängt Stream-Deck-Doppel-Requests und zu schnelles Hämmern ab,
        bevor überlappende Threads PT überfahren. Thread-sicher."""
        now = time.monotonic()
        with self._trigger_lock:
            last = self._last_trigger.get(key, 0.0)
            if now - last < interval:
                logging.info(f"Trigger '{key}' entprellt – Doppel-Trigger "
                             f"({now - last:.2f}s < {interval}s) ignoriert.")
                return False
            self._last_trigger[key] = now
            return True

    # ── Zentrale Befehls-API ───────────────────────────────────────────────
    #    EIN Dispatcher für ALLE Transporte (HTTP-Webtrigger, Unix-Socket und
    #    künftige). EIN Eintrag pro Funktion = die komplette API an einer
    #    Stelle. Nicht blockierend: die _trigger_*-Methoden starten ihre Worker
    #    selbst in Daemon-Threads; Preset/Vocaster sind kurze Operationen.
    #    Der Debounce sitzt bewusst HIER (bzw. in den _trigger_*-Methoden), also
    #    transport-unabhängig: derselbe Befehl von HTTP und Socket teilt sich
    #    denselben Entprell-Schlüssel.

    @objc.python_method
    def _command_table(self):
        """Baut (einmalig, gecacht) die Befehlstabelle: name → handler(args)→(ok,msg)."""
        tbl = getattr(self, "_cmd_table_cache", None)
        if tbl is not None:
            return tbl

        def _queue(fn, label):
            def _h(args):
                fn()
                return (True, f"{label} queued")
            return _h

        tbl = {
            "record_a":             _queue(self._trigger,                      "record_a"),
            "record_b":             _queue(self._trigger_b,                    "record_b"),
            "play":                 _queue(self._trigger_play,                 "play"),
            "play_custom":          _queue(self._trigger_play_custom,          "play_custom"),
            "goto_start":           _queue(self._trigger_start,                "goto_start"),
            "move_audio":           _queue(self._trigger_move_audio,           "move_audio"),
            "import":               _queue(self._trigger_import,               "import"),
            "export_wav":           _queue(self._trigger_export_wav,           "export_wav"),
            "export_aaf":           _queue(self._trigger_export_aaf,           "export_aaf"),
            "export_aaf_reference": _queue(self._trigger_export_aaf_reference, "export_aaf_reference"),
            "export_interplay":     _queue(self._trigger_export_interplay,     "export_interplay"),
            "preset":               self._cmd_preset,
            "vocaster_autogain_host":  lambda a: self._cmd_vocaster("autogain_host"),
            "vocaster_autogain_guest": lambda a: self._cmd_vocaster("autogain_guest"),
            "vocaster_phantom_on":     lambda a: self._cmd_vocaster("phantom_on"),
            "vocaster_phantom_off":    lambda a: self._cmd_vocaster("phantom_off"),
            "ping":   lambda a: (True, "pong"),
            "status": self._cmd_status,
            "list":   self._cmd_list,
        }
        self._cmd_table_cache = tbl
        return tbl

    @objc.python_method
    def command_dispatch(self, command, args=None):
        """Einziger Eintrittspunkt aller Transporte. Gibt (ok, message) zurück.
        `command` ist ein Befehlsname (Aliase werden aufgelöst), `args` eine
        optionale Liste von String-Argumenten (z. B. ["3"] für Preset 3)."""
        args = list(args or [])
        raw = (command or "").strip()
        cmd = _COMMAND_ALIASES.get(raw.lower(), raw.lower())
        handler = self._command_table().get(cmd)
        if handler is None:
            logging.info(f"Dispatch: unbekannter Befehl '{raw}'")
            return (False, f"unknown command: {raw}")
        try:
            ok, msg = handler(args)
            return (bool(ok), str(msg))
        except Exception as e:
            logging.exception(f"Dispatch '{cmd}' fehlgeschlagen")
            return (False, f"error: {e}")

    @objc.python_method
    def _cmd_preset(self, args):
        if not args:
            return (False, "preset needs a number, e.g. 'preset 3'")
        try:
            n = int(str(args[0]).strip())
        except (ValueError, TypeError):
            return (False, f"invalid preset number: {args[0]}")
        if not self._debounce_ok(f"preset:{n}", interval=0.6):
            return (True, f"preset {n} debounced")
        if self.load_preset_by_index(n - 1):
            return (True, f"preset {n} loaded")
        return (False, f"invalid preset index: {n}")

    @objc.python_method
    def _cmd_vocaster(self, action):
        voc = getattr(self, "vocaster", None)
        if voc is None:
            return (False, "no vocaster connected")
        if not self._debounce_ok(f"voc:{action}", interval=0.6):
            return (True, f"vocaster {action} debounced")
        try:
            if action == "autogain_host":
                voc.request_autogain(0, "Host")
            elif action == "autogain_guest":
                voc.request_autogain(1, "Guest")
            elif action == "phantom_on":
                voc.set_phantom(True)
            elif action == "phantom_off":
                voc.set_phantom(False)
            else:
                return (False, f"unknown vocaster action: {action}")
        except Exception as e:
            return (False, f"vocaster error: {e}")
        return (True, f"vocaster {action} queued")

    @objc.python_method
    def _cmd_status(self, args):
        port = getattr(self, "_http_port", self.settings.get("http_port", 8899))
        return (True, f"ok export_running={state.export_running} "
                      f"import_running={state.import_running} http_port={port}")

    @objc.python_method
    def _cmd_list(self, args):
        return (True, ",".join(sorted(self._command_table().keys())))

    def _trigger(self):
        if not self._debounce_ok("record_a"):
            return
        logging.info(">>> TRIGGER A <<<")
        tracks = self.settings.get("tracks", DEFAULT_SETTINGS["tracks"])
        monitor = self.settings.get("monitor_tracks", DEFAULT_SETTINGS["monitor_tracks"])
        threading.Thread(target=run_punch_in, args=(tracks, monitor), daemon=True).start()

    def _trigger_play(self):
        if not self._debounce_ok("play"):
            return
        logging.info(">>> TRIGGER PLAY <<<")
        threading.Thread(target=run_play, daemon=True).start()

    def _trigger_play_custom(self):
        if not self._debounce_ok("play_custom"):
            return
        logging.info(">>> TRIGGER PLAY CUSTOM <<<")
        threading.Thread(target=run_play_custom, daemon=True).start()

    def _trigger_start(self):
        if not self._debounce_ok("goto_start"):
            return
        threading.Thread(target=run_goto_start, daemon=True).start()

    def _trigger_move_audio(self):
        if not self._debounce_ok("move_audio"):
            return
        logging.info(">>> TRIGGER MOVE AUDIO <<<")
        src = self.settings.get("move_audio_source_tracks",
                                DEFAULT_SETTINGS["move_audio_source_tracks"])
        tgt = self.settings.get("move_audio_target_tracks",
                                DEFAULT_SETTINGS["move_audio_target_tracks"])
        threading.Thread(target=run_move_audio, args=(src, tgt), daemon=True).start()

    def _trigger_b(self):
        if not self._debounce_ok("record_b"):
            return
        logging.info(">>> TRIGGER B <<<")
        tracks = self.settings.get("tracks_b", DEFAULT_SETTINGS["tracks_b"])
        if not tracks:
            logging.warning("Profil B hat keine Spuren definiert – Trigger ignoriert.")
            return
        monitor = self.settings.get("monitor_tracks_b", DEFAULT_SETTINGS["monitor_tracks_b"])
        threading.Thread(target=run_punch_in, args=(tracks, monitor), daemon=True).start()

    def _trigger_import(self):
        if not self._debounce_ok("import", interval=1.0):
            return
        logging.info(">>> IMPORT TRIGGER <<<")
        threading.Thread(target=run_interplay_import, daemon=True).start()

    @objc.python_method
    def _begin_export_or_skip(self):
        """Gemeinsamer Export-Slot: nur EIN Export gleichzeitig – über ALLE
        Export-Arten (WAV/AAF/AAF-Ref/Interplay). Verhindert parallele
        Consolidate-/Export-Läufe auf dem geteilten gRPC-Kanal (sonst:
        'closed channel' / korrupte Selektion). True = darf starten."""
        with _export_lock:
            if state.export_running:
                logging.info("Export läuft bereits – Trigger ignoriert.")
                return False
            state.export_running = True
        return True

    @objc.python_method
    def _spawn_export(self, target, *args):
        """Startet einen Export-Worker im Thread und gibt den Export-Slot am
        Ende IMMER frei – auch bei Exception (sonst bliebe der Slot für immer
        gesperrt und der rote Punkt rot)."""
        def _work():
            try:
                target(*args)
            finally:
                with _export_lock:
                    state.export_running = False
                _set_busy(False)
        threading.Thread(target=_work, daemon=True).start()

    def _trigger_export_wav(self):
        if not self._debounce_ok("export_wav", interval=1.0):
            return
        if not self._begin_export_or_skip():
            return
        logging.info(">>> WAV EXPORT TRIGGER <<<")
        _set_busy(True)
        export_tracks = self.settings.get("export_tracks", DEFAULT_SETTINGS["export_tracks"])
        self._spawn_export(run_wav_export_standalone, export_tracks, self.settings)

    def _trigger_export_aaf(self):
        if not self._debounce_ok("export_aaf", interval=1.0):
            return
        if not self._begin_export_or_skip():
            return
        logging.info(">>> AAF EMBEDDED EXPORT TRIGGER <<<")
        _set_busy(True)
        export_tracks = self.settings.get("export_tracks", DEFAULT_SETTINGS["export_tracks"])
        self._spawn_export(run_aaf_export_standalone, export_tracks, self.settings)

    def _trigger_export_aaf_reference(self):
        if not self._debounce_ok("export_aaf_reference", interval=1.0):
            return
        if not self._begin_export_or_skip():
            return
        logging.info(">>> AAF REFERENCE EXPORT TRIGGER <<<")
        _set_busy(True)
        export_tracks = self.settings.get("export_tracks", DEFAULT_SETTINGS["export_tracks"])
        self._spawn_export(run_aaf_reference_export_standalone, export_tracks, self.settings)

    def _trigger_export_interplay(self):
        if not self._debounce_ok("export_interplay", interval=1.0):
            return
        if not self._begin_export_or_skip():
            return
        logging.info(">>> INTERPLAY EXPORT TRIGGER <<<")
        _set_busy(True)  # sofort sichtbar, noch vor Thread-Start (dispatcht intern auf Main-Thread)
        export_tracks = self.settings.get("export_tracks", DEFAULT_SETTINGS["export_tracks"])
        ws_steps = self.settings.get("interplay_workspace_steps", 17)
        self._spawn_export(run_interplay_export, export_tracks, self.settings, ws_steps)

    def update_menu_titles(self):
        """Updates all menu titles dynamically to reflect the current language."""
        self.start_item.title = t("record_a_start")
        self.start_b_item.title = t("record_b_start")
        self.play_item.title = t("play_input")
        self.play_custom_item.title = t("play_custom")
        self.wav_export_item.title = t("export_wav")
        self.aaf_export_item.title = t("export_aaf_embedded")
        self.aaf_ref_export_item.title = t("export_aaf_reference")
        self.interplay_export_item.title = t("export_interplay")
        self.import_item.title = t("interplay_import_start")
        self.settings_item.title = t("settings")
        self.refresh_tracks_item.title = t("refresh_tracks")
        self.open_log_item.title = t("open_log")
        self.export_menu.title = t("tab_export")
        self.help_menu.title = t("help_info")
        self.manual_item.title = t("manual")
        self.docs_item.title = t("tech_docs")
        self.credits_title.title = t("developed_by")
        self.opensource_item.title = t("opensource")
        self.quit_item.title = t("quit")

    # ── Menü-Callbacks ────────────────────────────────────────────────────

    def _on_start(self, _):
        logging.info(">>> MENU 'Record A starten' <<<")
        self._trigger()

    def _on_start_b(self, _):
        logging.info(">>> MENU 'Record B starten' <<<")
        self._trigger_b()

    def _on_start_play(self, _):
        logging.info(">>> MENU 'Play Input' <<<")
        self._trigger_play()

    def _on_start_play_custom(self, _):
        logging.info(t("log_menu_play_custom"))
        self._trigger_play_custom()

    def _on_move_audio(self, _):
        logging.info(">>> MENU 'Audio verschieben' <<<")
        self._trigger_move_audio()

    # ── Audio-verschieben Einstellungs-UI (dynamische Paar-Zeilen) ───────────
    @objc.python_method
    def _move_set(self, row, idx, val):
        """Aktualisiert eine Dropdown-Auswahl in der Paar-Liste."""
        if 0 <= row < len(self._move_pairs):
            self._move_pairs[row][idx] = val

    @objc.python_method
    def _move_add(self):
        """Fügt ein leeres Spurpaar hinzu und rendert neu."""
        self._move_pairs.append(["", ""])
        self._render_move_section()

    @objc.python_method
    def _move_remove(self, row):
        """Entfernt ein Spurpaar (mind. eine leere Zeile bleibt) und rendert neu."""
        if 0 <= row < len(self._move_pairs):
            del self._move_pairs[row]
        if not self._move_pairs:
            self._move_pairs = [["", ""]]
        self._render_move_section()

    @objc.python_method
    def _render_move_section(self):
        """(Neu-)Zeichnet die dynamischen Quelle→Ziel-Dropdown-Zeilen im
        Import-Tab. Quelle und Ziel sind paarweise – dadurch ist die Anzahl
        immer gleich. '+' fügt ein Paar hinzu, '−' entfernt eines."""
        view = self._move_t3_view
        PAD = 20
        # Alte dynamische Subviews entfernen
        for v in getattr(self, "_move_section_views", []):
            try:
                v.removeFromSuperview()
            except Exception:
                pass
        self._move_section_views = []

        opts = self._move_track_options
        y = self._move_rows_top_y

        def _popup(x, cur, tag, action_sel):
            o = list(opts)
            if cur and cur not in o:
                o.append(cur)
            p = AppKit.NSPopUpButton.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, 150, 24))
            p.removeAllItems()
            for it in o:
                p.addItemWithTitle_(it)
            if cur in o:
                p.selectItemWithTitle_(cur)
            p.setTag_(tag)
            p.setTarget_(self._move_target)
            p.setAction_(objc.selector(action_sel, signature=b'v@:@'))
            view.addSubview_(p)
            self._move_section_views.append(p)
            return p

        for i, (s_name, t_name) in enumerate(self._move_pairs):
            _popup(PAD, s_name, i, self._move_target.onSrc_)
            arrow = AppKit.NSTextField.labelWithString_("→")
            arrow.setFrame_(AppKit.NSMakeRect(PAD + 160, y + 2, 20, 20))
            view.addSubview_(arrow); self._move_section_views.append(arrow)
            _popup(PAD + 200, t_name, i, self._move_target.onTgt_)
            minus = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(PAD + 360, y, 30, 24))
            minus.setTitle_("−"); minus.setBezelStyle_(AppKit.NSBezelStyleRounded)
            minus.setTag_(i); minus.setTarget_(self._move_target)
            minus.setAction_(objc.selector(self._move_target.onRemove_, signature=b'v@:@'))
            view.addSubview_(minus); self._move_section_views.append(minus)
            y -= 30

        plus = AppKit.NSButton.alloc().initWithFrame_(AppKit.NSMakeRect(PAD, y - 4, 170, 26))
        plus.setTitle_(t("btn_move_add_pair")); plus.setBezelStyle_(AppKit.NSBezelStyleRounded)
        plus.setTarget_(self._move_target)
        plus.setAction_(objc.selector(self._move_target.onAdd_, signature=b'v@:@'))
        view.addSubview_(plus); self._move_section_views.append(plus)

    def _on_start_import(self, _):
        logging.info(">>> MENU 'Interplay Import starten' <<<")
        self._trigger_import()

    def _on_quit(self, _):
        logging.info(">>> MENU 'Beenden' <<<")
        _cleanup_watchdog()
        self._close_unix_socket()   # Socket-Datei sofort entfernen, nicht erst via atexit
        _close_engine()
        logging.info("PunchBuddy beendet.")
        rumps.quit_application()

    def _copy_url(self, path, label="URL"):
        """Generische Methode: kopiert eine Trigger-URL in die Zwischenablage."""
        port = getattr(self, '_http_port', 8899)
        url = f"http://127.0.0.1:{port}{path}"
        try:
            subprocess.run(["pbcopy"], input=url, universal_newlines=True)
            rumps.notification("PunchBuddy", "Kopiert", f"{label}: {url}")
        except Exception:
            rumps.alert(label, f"URL:\n{url}")


    def _on_unified_settings(self, _):
        """Öffnet das kombinierte Einstellungs- und Spurenauswahl-Fenster."""
        _ensure_dock_icon()
        if not APPKIT_OK:
            rumps.alert(t("alert_error"), "AppKit nicht verfügbar.")
            return

        # Spuren aus Pro Tools lesen
        track_names = []
        try:
            engine = _get_engine()
            if engine is not None:
                ok, all_tracks = _ptsl_call(engine.track_list, label="SettingsTrackList", timeout=5.0)
                if ok and all_tracks:
                    track_names = [t.name for t in all_tracks]
        except Exception as e:
            logging.warning(f"Konnte Spuren nicht aus Pro Tools lesen: {e}")

        try:
            self._open_unified_settings_window(track_names)
        except Exception as e:
            logging.error(f"Fehler beim Oeffnen des Fensters: {e}", exc_info=True)
            rumps.alert(t("alert_error"), f"Fenster konnte nicht geoeffnet werden:\n{e}")

    def _on_refresh_tracks(self, _):
        logging.info(">>> MENU 'Spuren neu einlesen' <<<")
        _invalidate_session_tracks()
        eng = _get_engine()
        if eng is None:
            rumps.alert(t("alert_error"), "PTSL nicht verfügbar.")
            return
        ok = refresh_session_tracks(eng)
        if ok:
            n = cached_track_count()
            rumps.notification("PunchBuddy", t("refresh_tracks"), f"{n} Spuren eingelesen.")
        else:
            rumps.alert(t("alert_error"), "Spuren konnten nicht gelesen werden.")

    def _on_open_log(self, _):
        logging.info(">>> MENU 'Log File oeffnen' <<<")
        if os.path.exists(LOG_PATH):
            subprocess.Popen(["open", LOG_PATH])
        else:
            rumps.alert(t("alert_note"), t("msg_log_not_created"))

    def _on_start_export_wav(self, _):
        logging.info(">>> MENU 'WAV Export' <<<")
        self._trigger_export_wav()

    def _on_start_export_aaf(self, _):
        logging.info(">>> MENU 'AAF Export (Embedded)' <<<")
        self._trigger_export_aaf()

    def _on_start_export_aaf_reference(self, _):
        logging.info(">>> MENU 'AAF Export (Reference)' <<<")
        self._trigger_export_aaf_reference()

    def _on_start_export_interplay(self, _):
        logging.info(">>> MENU 'Interplay Export' <<<")
        self._trigger_export_interplay()

    def _on_open_general_settings(self, _):
        _ensure_dock_icon()
        if not APPKIT_OK:
            rumps.alert("Fehler", "AppKit nicht verfügbar – natives Fenster kann nicht geöffnet werden.")
            return
        
        try:
            self._open_settings_window()
        except Exception as e:
            logging.error(f"Fehler beim Oeffnen des Einstellungsfensters: {e}", exc_info=True)
            rumps.alert("Fehler", f"Fenster konnte nicht geoeffnet werden:\n{e}")

    def _open_settings_window(self):
        if _close_existing_settings_window():
            return
        NSWindow          = AppKit.NSWindow
        NSView            = AppKit.NSView
        NSButton          = AppKit.NSButton
        NSTextField       = AppKit.NSTextField
        NSFont            = AppKit.NSFont
        NSMakeRect        = AppKit.NSMakeRect
        NSOnState         = AppKit.NSOnState
        NSOffState        = AppKit.NSOffState
        NSBezelStyleRounded = AppKit.NSBezelStyleRounded

        WIN_W = 400
        WIN_H = 580
        PAD = 20

        rect = NSMakeRect(0, 0, WIN_W, WIN_H)
        style = (
            AppKit.NSWindowStyleMaskTitled |
            AppKit.NSWindowStyleMaskClosable
        )
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            style,
            AppKit.NSBackingStoreBuffered,
            False
        )
        window.setReleasedWhenClosed_(False)  # Lebenszeit über _config_refs steuern
        window.setTitle_("Einstellungen PunchBuddy")
        window.setLevel_(3)

        # Fenster-Icon setzen
        _win_icon = _load_ns_icon()
        if _win_icon:
            _win_icon.setSize_(AppKit.NSMakeSize(32, 32))
            window.setRepresentedURL_(AppKit.NSURL.URLWithString_("punchbuddy://settings"))
            window.standardWindowButton_(AppKit.NSWindowDocumentIconButton).setImage_(_win_icon)

        content = window.contentView()
        content.setWantsLayer_(True)

        target = _SettingsButtonTarget.alloc().init()
        controls = {}

        # ── Loudness Section ──
        y = WIN_H - 40
        lbl = NSTextField.labelWithString_("Lautheitskorrektur (EBU R128):")
        lbl.setFrame_(NSMakeRect(PAD, y, 200, 20))
        lbl.setFont_(NSFont.boldSystemFontOfSize_(12))
        content.addSubview_(lbl)

        y -= 25
        cb_loudness = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y, 20, 20))
        cb_loudness.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb_loudness.setTitle_("")
        cb_loudness.setState_(NSOnState if self.settings.get("loudness_enabled", True) else NSOffState)
        content.addSubview_(cb_loudness)
        controls["loudness_enabled"] = cb_loudness
        lbl_cb1 = NSTextField.labelWithString_("Aktivieren")
        lbl_cb1.setFrame_(NSMakeRect(PAD + 25, y+2, 100, 20))
        content.addSubview_(lbl_cb1)

        y -= 25
        lbl_lufs = NSTextField.labelWithString_("Ziel-Lautheit (LUFS):")
        lbl_lufs.setFrame_(NSMakeRect(PAD, y, 150, 20))
        content.addSubview_(lbl_lufs)
        tf_lufs = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, y, 80, 22))
        tf_lufs.setStringValue_(str(self.settings.get("target_lufs", -23.0)))
        content.addSubview_(tf_lufs)
        controls["target_lufs"] = tf_lufs

        y -= 25
        lbl_tp = NSTextField.labelWithString_("Max True Peak (dB):")
        lbl_tp.setFrame_(NSMakeRect(PAD, y, 150, 20))
        content.addSubview_(lbl_tp)
        tf_tp = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, y, 80, 22))
        tf_tp.setStringValue_(str(self.settings.get("max_truepeak", -3.0)))
        content.addSubview_(tf_tp)
        controls["max_truepeak"] = tf_tp

        # ── Export Einstellungen Allgemein ──
        y -= 40
        lbl_tc = NSTextField.labelWithString_("Export Einstellungen Allgemein:")
        lbl_tc.setFrame_(NSMakeRect(PAD, y, 250, 20))
        lbl_tc.setFont_(NSFont.boldSystemFontOfSize_(12))
        content.addSubview_(lbl_tc)

        y -= 25
        lbl_vt = NSTextField.labelWithString_("Video-Spurname:")
        lbl_vt.setFrame_(NSMakeRect(PAD, y, 150, 20))
        content.addSubview_(lbl_vt)
        tf_vt = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, y, 180, 22))
        tf_vt.setStringValue_(str(self.settings.get("video_track", "Video 1")))
        content.addSubview_(tf_vt)
        controls["video_track"] = tf_vt

        y -= 25
        lbl_tc2 = NSTextField.labelWithString_("Start-Timecode (HH:MM:SS:FF):")
        lbl_tc2.setFrame_(NSMakeRect(PAD, y, 200, 20))
        content.addSubview_(lbl_tc2)
        tf_tc = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 200, y, 120, 22))
        tf_tc.setStringValue_(str(self.settings.get("export_start_tc", "10:00:00:00")))
        content.addSubview_(tf_tc)
        controls["export_start_tc"] = tf_tc

        y -= 25
        lbl_ext = NSTextField.labelWithString_("Shift+Ö Anzahl (Spurausdehnung):")
        lbl_ext.setFrame_(NSMakeRect(PAD, y, 210, 20))
        content.addSubview_(lbl_ext)
        tf_ext = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 210, y, 60, 22))
        tf_ext.setStringValue_(str(self.settings.get("extend_count", 7)))
        content.addSubview_(tf_ext)
        controls["extend_count"] = tf_ext

        y -= 25
        cb_wav = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y, 20, 20))
        cb_wav.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb_wav.setTitle_("")
        cb_wav.setState_(NSOnState if self.settings.get("wav_export_enabled", False) else NSOffState)
        content.addSubview_(cb_wav)
        controls["wav_export_enabled"] = cb_wav
        lbl_wav = NSTextField.labelWithString_("WAV Export aktivieren")
        lbl_wav.setFrame_(NSMakeRect(PAD + 25, y+2, 200, 20))
        content.addSubview_(lbl_wav)

        y -= 25
        cb_aaf = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y, 20, 20))
        cb_aaf.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb_aaf.setTitle_("")
        cb_aaf.setState_(NSOnState if self.settings.get("aaf_export_enabled", False) else NSOffState)
        content.addSubview_(cb_aaf)
        controls["aaf_export_enabled"] = cb_aaf
        lbl_aaf = NSTextField.labelWithString_("AAF Export aktivieren (Embedded Audio)")
        lbl_aaf.setFrame_(NSMakeRect(PAD + 25, y+2, 250, 20))
        content.addSubview_(lbl_aaf)

        # ── Interplay Section ──
        y -= 40
        lbl_ip = NSTextField.labelWithString_("Avid Interplay NEXIS Export:")
        lbl_ip.setFrame_(NSMakeRect(PAD, y, 200, 20))
        lbl_ip.setFont_(NSFont.boldSystemFontOfSize_(12))
        content.addSubview_(lbl_ip)

        y -= 25
        cb_interplay = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y, 20, 20))
        cb_interplay.setButtonType_(AppKit.NSButtonTypeSwitch)
        cb_interplay.setTitle_("")
        cb_interplay.setState_(NSOnState if self.settings.get("interplay_enabled", False) else NSOffState)
        content.addSubview_(cb_interplay)
        controls["interplay_enabled"] = cb_interplay
        lbl_cb2 = NSTextField.labelWithString_("Aktivieren")
        lbl_cb2.setFrame_(NSMakeRect(PAD + 25, y+2, 100, 20))
        content.addSubview_(lbl_cb2)

        y -= 25
        lbl_ws = NSTextField.labelWithString_("Workspace Name:")
        lbl_ws.setFrame_(NSMakeRect(PAD, y, 150, 20))
        content.addSubview_(lbl_ws)
        tf_ws = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, y, 180, 22))
        tf_ws.setStringValue_(str(self.settings.get("interplay_workspace", "001-aktuelles [fad-nexis]")))
        content.addSubview_(tf_ws)
        controls["interplay_workspace"] = tf_ws

        y -= 25
        lbl_steps = NSTextField.labelWithString_("Workspace Position (Steps):")
        lbl_steps.setFrame_(NSMakeRect(PAD, y, 170, 20))
        content.addSubview_(lbl_steps)
        tf_steps = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 170, y, 60, 22))
        tf_steps.setStringValue_(str(self.settings.get("interplay_workspace_steps", 17)))
        content.addSubview_(tf_steps)
        controls["interplay_workspace_steps"] = tf_steps

        # ── Buttons ──
        target.setup(self, controls, window)

        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, 20, 100, 30))
        cancel_btn.setTitle_("Abbrechen")
        cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        cancel_btn.setTarget_(target)
        cancel_btn.setAction_(objc.selector(target.onCancel_, signature=b'v@:@'))
        content.addSubview_(cancel_btn)

        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(WIN_W - 120, 20, 100, 30))
        save_btn.setTitle_("Speichern")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setKeyEquivalent_("\r")
        save_btn.setTarget_(target)
        save_btn.setAction_(objc.selector(target.onSave_, signature=b'v@:@'))
        content.addSubview_(save_btn)

        global _config_refs
        _config_refs = [window, target, controls]
        window.makeKeyAndOrderFront_(None)
        window.center()

    # ── Hilfe & Info Callbacks ───────────────────────────────────────────
    
    @objc.python_method
    def _doc_path(self, filename):
        """Sucht eine Dokumentationsdatei – zuerst IN der App gebündelt
        (PyInstaller _MEIPASS), dann neben der App, dann neben dem Skript.

        Die gebündelte Variante ist entscheidend: nur sie funktioniert auch,
        wenn der User PunchBuddy.app aus dem DMG nach /Programme zieht und die
        Doku-Dateien dabei zurücklässt (früher war der Link dann tot)."""
        import sys as _sys
        candidates = []
        # 1. In der App gebündelt (--add-data) – immer vorhanden
        meipass = getattr(_sys, '_MEIPASS', None)
        if meipass:
            candidates.append(os.path.join(meipass, filename))
        if getattr(_sys, 'frozen', False):
            # 2. Neben PunchBuddy.app (im DMG-Ordner)
            app_dir = os.path.dirname(os.path.dirname(os.path.dirname(_sys.executable)))
            candidates.append(os.path.join(os.path.dirname(app_dir), filename))
        # 3. Neben dem Skript (Entwicklung)
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), filename))
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    @objc.python_method
    def _localized_doc(self, base):
        """Sucht die zur eingestellten Sprache passende Doku-Variante
        (z. B. 'PunchBuddy_Anleitung_en.html'); fällt auf die deutsche
        Basisdatei ('PunchBuddy_Anleitung.html') zurück, falls es keine
        Übersetzung gibt."""
        lang = (self.settings.get("language", "de") or "de")
        if lang != "de":
            p = self._doc_path(f"{base}_{lang}.html")
            if p:
                return p
        return self._doc_path(f"{base}.html")

    # ── Stream-Deck-Assets (mitgeliefert) ───────────────────────────────────
    @objc.python_method
    def _streamdeck_plugin_path(self):
        """Findet die mitgelieferte .streamDeckPlugin-Datei (in der App gebündelt
        via PyInstaller, sonst im Repo unter streamdeck/plugin/)."""
        import sys as _sys
        name = "com.punchbuddy.control.streamDeckPlugin"
        candidates = []
        meipass = getattr(_sys, "_MEIPASS", None)
        if meipass:
            candidates.append(os.path.join(meipass, "streamdeck", name))
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "streamdeck", "plugin", name))
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

    @objc.python_method
    def _install_streamdeck_plugin(self):
        p = self._streamdeck_plugin_path()
        if not p:
            rumps.alert("Stream Deck", "Plugin-Datei nicht gefunden (nicht mitgeliefert).")
            return
        try:
            subprocess.Popen(["open", p])   # Stream Deck zeigt seinen Installations-Dialog
            rumps.notification("PunchBuddy", "Stream Deck",
                               "Plugin-Installation gestartet – im Stream Deck bestätigen.")
        except Exception as e:
            rumps.alert("Stream Deck", f"Konnte Plugin nicht öffnen:\n{e}")

    @objc.python_method
    def _reveal_streamdeck_plugin(self):
        p = self._streamdeck_plugin_path()
        if not p:
            rumps.alert("Stream Deck", "Plugin-Datei nicht gefunden (nicht mitgeliefert).")
            return
        try:
            dest = os.path.join(os.path.expanduser("~/Downloads"), os.path.basename(p))
            shutil.copy2(p, dest)
            subprocess.Popen(["open", "-R", dest])   # im Finder markieren
            rumps.notification("PunchBuddy", "Stream Deck", f"Plugin gespeichert: {dest}")
        except Exception as e:
            rumps.alert("Stream Deck", f"Konnte Plugin nicht speichern:\n{e}")

    @objc.python_method
    def _generate_streamdeck_launchers(self):
        sock = self.settings.get("unix_socket_path", DEFAULT_UNIX_SOCKET) or DEFAULT_UNIX_SOCKET
        dest = os.path.expanduser("~/Applications/PunchBuddy Launchers")
        try:
            n = _build_streamdeck_launchers(dest, sock)
            subprocess.Popen(["open", dest])
            rumps.notification("PunchBuddy", "Stream Deck",
                               f"{n} Tasten-Launcher erzeugt in {dest}")
        except Exception as e:
            rumps.alert("Stream Deck", f"Konnte Launcher nicht erzeugen:\n{e}")

    def _on_help_manual(self, _):
        import subprocess as _sp
        _doc = self._localized_doc("PunchBuddy_Anleitung")
        if _doc:
            try:
                _sp.Popen(["open", _doc]); return
            except Exception:
                pass
        rumps.alert(title=t("msg_manual_not_found_title"),
                    message=t("msg_manual_not_found_body"))

    def _on_help_docs(self, _):
        import subprocess as _sp
        _doc = self._localized_doc("PunchBuddy_Technische_Doku")
        if _doc:
            try:
                _sp.Popen(["open", _doc]); return
            except Exception:
                pass
        rumps.alert(title=t("msg_docs_not_found_title"),
                    message=t("msg_docs_not_found_body"))

    def _on_help_opensource(self, _):
        rumps.alert(
            title=t("msg_opensource_title"),
            message=t("msg_opensource_body")
        )





    # ── Kombiniertes Einstellungsfenster (Tab-basiert) ─────────────────────

    def _open_unified_settings_window(self, track_names):
        """Erstellt ein Tab-basiertes Fenster mit Spurenauswahl + Erweiterte Einstellungen."""
        if _close_existing_settings_window():
            return
        NSWindow          = AppKit.NSWindow
        NSView            = AppKit.NSView
        NSButton          = AppKit.NSButton
        NSTextField       = AppKit.NSTextField
        NSScrollView      = AppKit.NSScrollView
        NSTabView         = AppKit.NSTabView
        NSTabViewItem     = AppKit.NSTabViewItem
        NSFont            = AppKit.NSFont
        NSColor           = AppKit.NSColor
        NSMakeRect        = AppKit.NSMakeRect
        NSScreen          = AppKit.NSScreen
        NSOnState         = AppKit.NSOnState
        NSOffState        = AppKit.NSOffState
        NSBezelStyleRounded = AppKit.NSBezelStyleRounded

        WIN_W = 680
        WIN_H = 1020
        PAD = 20
        BUTTON_H = 50

        screen = NSScreen.mainScreen().frame()
        xx = (screen.size.width - WIN_W) / 2
        yy = (screen.size.height - WIN_H) / 2

        style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(xx, yy, WIN_W, WIN_H), style, AppKit.NSBackingStoreBuffered, False)
        window.setReleasedWhenClosed_(False)  # Lebenszeit über _config_refs steuern
        window.setTitle_(t("title_settings_window"))
        window.setLevel_(3)

        # Fenster-Icon setzen
        _win_icon = _load_ns_icon()
        if _win_icon:
            _win_icon.setSize_(AppKit.NSMakeSize(32, 32))
            window.setRepresentedURL_(AppKit.NSURL.URLWithString_("punchbuddy://settings"))
            window.standardWindowButton_(AppKit.NSWindowDocumentIconButton).setImage_(_win_icon)

        content = window.contentView()
        content.setWantsLayer_(True)

        tab_view = NSTabView.alloc().initWithFrame_(
            NSMakeRect(10, BUTTON_H + 10, WIN_W - 20, WIN_H - BUTTON_H - 20))

        # ── TAB 1: SPUREN ────────────────────────────────────────────────
        tab1 = NSTabViewItem.alloc().initWithIdentifier_("Spuren")
        tab1.setLabel_(t("tab_tracks"))
        t1_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab1.setView_(t1_view)

        checkboxes = {}

        if track_names:
            rec_a_set  = set(self.settings.get("tracks", []))
            mon_a_set  = set(self.settings.get("monitor_tracks", []))
            rec_b_set  = set(self.settings.get("tracks_b", []))
            mon_b_set  = set(self.settings.get("monitor_tracks_b", []))
            export_set = set(self.settings.get("export_tracks", []))
            loud_set   = set(self.settings.get("loudness_tracks", []))
            play_set   = set(self.settings.get("play_monitor_tracks", []))

            ROW_H = 28; LABEL_W = 180; CB_W = 55; GAP = 10
            col_a_x      = LABEL_W + 5
            col_b_x      = col_a_x + CB_W + GAP
            col_export_x = col_b_x + CB_W + GAP + 10
            col_loud_x   = col_export_x + CB_W + GAP
            col_play_x   = col_loud_x + CB_W + GAP

            HEADER_H = 65
            scroll_h = t1_view.frame().size.height - HEADER_H

            # Column Headers
            for txt, cx, w in [(t("col_record_a"), col_a_x, CB_W), (t("col_record_b"), col_b_x, CB_W),
                                (t("col_export"), col_export_x, CB_W), (t("col_loudness"), col_loud_x, CB_W + 15),
                                (t("col_play_input"), col_play_x, CB_W + 20)]:
                lbl = NSTextField.labelWithString_(txt)
                lbl.setFrame_(NSMakeRect(cx, scroll_h + 15, w, 18))
                lbl.setFont_(NSFont.boldSystemFontOfSize_(11))
                lbl.setAlignment_(AppKit.NSTextAlignmentCenter)
                t1_view.addSubview_(lbl)

            lbl_spur = NSTextField.labelWithString_(t("col_track"))
            lbl_spur.setFrame_(NSMakeRect(PAD, scroll_h + 15, LABEL_W, 18))
            lbl_spur.setFont_(NSFont.boldSystemFontOfSize_(11))
            t1_view.addSubview_(lbl_spur)

            # ScrollView
            scroll_view = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(0, 0, t1_view.frame().size.width, scroll_h))
            scroll_view.setHasVerticalScroller_(True)
            scroll_view.setAutohidesScrollers_(True)
            scroll_view.setBorderType_(AppKit.NSBezelBorder)

            doc_h = len(track_names) * ROW_H + PAD
            doc_view = NSView.alloc().initWithFrame_(
                NSMakeRect(0, 0, t1_view.frame().size.width - 20, doc_h))

            for i, name in enumerate(track_names):
                row_y = doc_h - (i + 1) * ROW_H
                lbl = NSTextField.labelWithString_(name)
                lbl.setFrame_(NSMakeRect(PAD, row_y + 4, LABEL_W - PAD, 20))
                lbl.setFont_(NSFont.systemFontOfSize_(12))
                doc_view.addSubview_(lbl)
                if i % 2 == 0:
                    stripe = AppKit.NSBox.alloc().initWithFrame_(
                        NSMakeRect(0, row_y, t1_view.frame().size.width, ROW_H))
                    stripe.setBoxType_(AppKit.NSBoxCustom)
                    stripe.setBorderType_(AppKit.NSNoBorder)
                    stripe.setFillColor_(NSColor.quaternaryLabelColor())
                    doc_view.addSubview_positioned_relativeTo_(stripe, AppKit.NSWindowBelow, lbl)
                cbs = {}
                for key, cx, active_set in [
                    ("rec_a",    col_a_x,     rec_a_set),
                    ("rec_b",    col_b_x,     rec_b_set),
                    ("export",   col_export_x, export_set),
                    ("loud",     col_loud_x,      loud_set),
                    ("play_mon", col_play_x,      play_set),
                ]:
                    cb = NSButton.alloc().initWithFrame_(NSMakeRect(cx + 15, row_y + 3, 22, 22))
                    cb.setButtonType_(AppKit.NSButtonTypeSwitch)
                    cb.setTitle_("")
                    cb.setState_(NSOnState if name in active_set else NSOffState)
                    doc_view.addSubview_(cb)
                    cbs[key] = cb
                checkboxes[name] = cbs
            scroll_view.setDocumentView_(doc_view)
            t1_view.addSubview_(scroll_view)
        else:
            nl = NSTextField.labelWithString_(t("msg_no_tracks"))
            nl.setFrame_(NSMakeRect(PAD, t1_view.frame().size.height / 2, WIN_W - 40, 30))
            t1_view.addSubview_(nl)

        # ── TAB 2: EXPORT ────────────────────────────────────────────────
        tab2 = NSTabViewItem.alloc().initWithIdentifier_("Export")
        tab2.setLabel_(t("tab_export"))
        t2_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab2.setView_(t2_view)

        controls = {}
        t2_y = t2_view.frame().size.height - 35

        # ── Rubrik 1: Export Einstellungen Allgemein ──
        h2 = NSTextField.labelWithString_(t("lbl_export_settings_general"))
        h2.setFrame_(NSMakeRect(PAD, t2_y, 350, 20))
        h2.setFont_(NSFont.boldSystemFontOfSize_(13))
        t2_view.addSubview_(h2)

        t2_y -= 28
        ltc = NSTextField.labelWithString_(t("lbl_start_tc"))
        ltc.setFrame_(NSMakeRect(PAD, t2_y, 220, 20)); t2_view.addSubview_(ltc)
        tf_tc = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 120, 22))
        tf_tc.setStringValue_(str(self.settings.get("export_start_tc", "10:00:00:00")))
        t2_view.addSubview_(tf_tc); controls["export_start_tc"] = tf_tc

        t2_y -= 25
        lvt = NSTextField.labelWithString_(t("lbl_video_track"))
        lvt.setFrame_(NSMakeRect(PAD, t2_y, 220, 20)); t2_view.addSubview_(lvt)
        tf_vt2 = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 120, 22))
        tf_vt2.setStringValue_(str(self.settings.get("video_track", "Video 1")))
        t2_view.addSubview_(tf_vt2); controls["video_track"] = tf_vt2

        t2_y -= 30
        cb_l = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, 20, 20))
        cb_l.setButtonType_(AppKit.NSButtonTypeSwitch); cb_l.setTitle_("")
        cb_l.setState_(NSOnState if self.settings.get("loudness_enabled", True) else NSOffState)
        t2_view.addSubview_(cb_l); controls["loudness_enabled"] = cb_l
        ll = NSTextField.labelWithString_(t("lbl_loudness_enable"))
        ll.setFrame_(NSMakeRect(PAD + 25, t2_y + 2, 300, 20)); t2_view.addSubview_(ll)

        t2_y -= 25
        ll2 = NSTextField.labelWithString_(t("lbl_target_lufs"))
        ll2.setFrame_(NSMakeRect(PAD + 25, t2_y, 170, 20)); t2_view.addSubview_(ll2)
        tf_lufs = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 80, 22))
        tf_lufs.setStringValue_(str(self.settings.get("target_lufs", -23.0)))
        t2_view.addSubview_(tf_lufs); controls["target_lufs"] = tf_lufs

        t2_y -= 25
        ll3 = NSTextField.labelWithString_(t("lbl_max_truepeak"))
        ll3.setFrame_(NSMakeRect(PAD + 25, t2_y, 170, 20)); t2_view.addSubview_(ll3)
        tf_tp = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 80, 22))
        tf_tp.setStringValue_(str(self.settings.get("max_truepeak", -3.0)))
        t2_view.addSubview_(tf_tp); controls["max_truepeak"] = tf_tp

        # ── Export-Pfade (leer = Standard <Session>/export) ──
        t2_y -= 26
        h_paths = NSTextField.labelWithString_(t("lbl_export_paths"))
        h_paths.setFrame_(NSMakeRect(PAD, t2_y, WIN_W - PAD * 2, 20))
        h_paths.setFont_(NSFont.boldSystemFontOfSize_(12))
        t2_view.addSubview_(h_paths)

        self._export_path_browse_buttons = []
        for _key, _lblkey in (("wav_export_path", "lbl_wav_path"),
                              ("aaf_embedded_export_path", "lbl_aaf_emb_path"),
                              ("aaf_reference_export_path", "lbl_aaf_ref_path")):
            t2_y -= 26
            _l = NSTextField.labelWithString_(t(_lblkey))
            _l.setFrame_(NSMakeRect(PAD, t2_y + 2, 150, 18))
            _l.setFont_(NSFont.systemFontOfSize_(11))
            t2_view.addSubview_(_l)
            _tf = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 155, t2_y, 290, 22))
            _tf.setStringValue_(self.settings.get(_key, "") or "")
            _tf.setFont_(NSFont.systemFontOfSize_(11))
            t2_view.addSubview_(_tf); controls[_key] = _tf
            _btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 450, t2_y, 110, 22))
            _btn.setTitle_(t("btn_browse"))
            _btn.setBezelStyle_(NSBezelStyleRounded)
            _btn.setFont_(NSFont.systemFontOfSize_(11))
            _btn.setTag_(len(self._export_path_browse_buttons))
            t2_view.addSubview_(_btn)
            self._export_path_browse_buttons.append((_btn, _tf))

        # Browse-Buttons mit NSOpenPanel-Target verdrahten
        _br_target = _ExportPathBrowseTarget.alloc().init()
        _br_target._fields = [tf for (_b, tf) in self._export_path_browse_buttons]
        for _b, _tf in self._export_path_browse_buttons:
            _b.setTarget_(_br_target)
            _b.setAction_(objc.selector(_br_target.onBrowse_, signature=b'v@:@'))
        self._export_path_browse_target = _br_target

        # ── Trennlinie ──
        t2_y -= 20
        sep2 = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, WIN_W - PAD * 2 - 40, 1))
        sep2.setBoxType_(AppKit.NSBoxSeparator)
        t2_view.addSubview_(sep2)

        # ── Rubrik 2: Interplay Export Einstellungen ──
        t2_y -= 25
        h3 = NSTextField.labelWithString_(t("lbl_interplay_export_settings"))
        h3.setFrame_(NSMakeRect(PAD, t2_y, 350, 20))
        h3.setFont_(NSFont.boldSystemFontOfSize_(13))
        t2_view.addSubview_(h3)

        t2_y -= 28
        lst = NSTextField.labelWithString_(t("lbl_workspace_steps"))
        lst.setFrame_(NSMakeRect(PAD, t2_y, 200, 20)); t2_view.addSubview_(lst)
        tf_st = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 230, t2_y, 60, 22))
        tf_st.setStringValue_(str(self.settings.get("interplay_workspace_steps", 17)))
        t2_view.addSubview_(tf_st); controls["interplay_workspace_steps"] = tf_st

        t2_y -= 28
        l_ekw = NSTextField.labelWithString_(t("lbl_error_keywords"))
        l_ekw.setFrame_(NSMakeRect(PAD, t2_y, 145, 20)); t2_view.addSubview_(l_ekw)
        tf_ekw = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, t2_y, 400, 22))
        tf_ekw.setStringValue_(self.settings.get("export_error_keywords",
            "error,fail,fehler,unsuccessful,could not,unable,problem,warning,aborted,abgebrochen"))
        tf_ekw.setToolTip_("Kommagetrennte Schlüsselwörter – bei Treffer wird der Export als fehlgeschlagen gewertet")
        t2_view.addSubview_(tf_ekw); controls["export_error_keywords"] = tf_ekw

        t2_y -= 25
        l_skw = NSTextField.labelWithString_(t("lbl_success_keywords"))
        l_skw.setFrame_(NSMakeRect(PAD, t2_y, 145, 20)); t2_view.addSubview_(l_skw)
        tf_skw = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 150, t2_y, 400, 22))
        tf_skw.setStringValue_(self.settings.get("export_success_keywords",
            "success,complete,finished,done,exported,erfolgreich,abgeschlossen,fertig"))
        tf_skw.setToolTip_("Kommagetrennte Schlüsselwörter – bei Treffer wird der Export als erfolgreich gewertet")
        t2_view.addSubview_(tf_skw); controls["export_success_keywords"] = tf_skw

        # ── Trennlinie ──
        t2_y -= 20
        sep3 = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, WIN_W - PAD * 2 - 40, 1))
        sep3.setBoxType_(AppKit.NSBoxSeparator)
        t2_view.addSubview_(sep3)

        # ── Rubrik 4: Rename Sequence ──
        t2_y -= 25
        h4 = NSTextField.labelWithString_(t("lbl_rename_seq"))
        h4.setFrame_(NSMakeRect(PAD, t2_y, 420, 20))
        h4.setFont_(NSFont.boldSystemFontOfSize_(13))
        t2_view.addSubview_(h4)

        t2_y -= 28
        cb_rn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, 20, 20))
        cb_rn.setButtonType_(AppKit.NSButtonTypeSwitch); cb_rn.setTitle_("")
        cb_rn.setState_(NSOnState if self.settings.get("interplay_rename_enabled", False) else NSOffState)
        t2_view.addSubview_(cb_rn); controls["interplay_rename_enabled"] = cb_rn
        l_rn = NSTextField.labelWithString_(t("lbl_rename_enable"))
        l_rn.setFrame_(NSMakeRect(PAD + 25, t2_y + 2, 350, 20)); t2_view.addSubview_(l_rn)

        t2_y -= 25
        l_ts2 = NSTextField.labelWithString_(t("lbl_rename_trim_start"))
        l_ts2.setFrame_(NSMakeRect(PAD + 25, t2_y, 200, 20)); t2_view.addSubview_(l_ts2)
        tf_ts = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 260, t2_y, 60, 22))
        tf_ts.setStringValue_(str(self.settings.get("interplay_rename_trim_start", 0)))
        t2_view.addSubview_(tf_ts); controls["interplay_rename_trim_start"] = tf_ts

        t2_y -= 25
        l_te2 = NSTextField.labelWithString_(t("lbl_rename_trim_end"))
        l_te2.setFrame_(NSMakeRect(PAD + 25, t2_y, 200, 20)); t2_view.addSubview_(l_te2)
        tf_te = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 260, t2_y, 60, 22))
        tf_te.setStringValue_(str(self.settings.get("interplay_rename_trim_end", 0)))
        t2_view.addSubview_(tf_te); controls["interplay_rename_trim_end"] = tf_te

        t2_y -= 25
        l_pf2 = NSTextField.labelWithString_(t("lbl_rename_prefix"))
        l_pf2.setFrame_(NSMakeRect(PAD + 25, t2_y, 150, 20)); t2_view.addSubview_(l_pf2)
        tf_pf = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 260, t2_y, 160, 22))
        tf_pf.setStringValue_(self.settings.get("interplay_rename_prefix", ""))
        t2_view.addSubview_(tf_pf); controls["interplay_rename_prefix"] = tf_pf

        t2_y -= 25
        l_sf2 = NSTextField.labelWithString_(t("lbl_rename_suffix"))
        l_sf2.setFrame_(NSMakeRect(PAD + 25, t2_y, 150, 20)); t2_view.addSubview_(l_sf2)
        tf_sf = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 260, t2_y, 160, 22))
        tf_sf.setStringValue_(self.settings.get("interplay_rename_suffix", ""))
        t2_view.addSubview_(tf_sf); controls["interplay_rename_suffix"] = tf_sf

        # ── Trennlinie ──
        t2_y -= 25
        sep_lang = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t2_y, WIN_W - PAD * 2 - 40, 1))
        sep_lang.setBoxType_(AppKit.NSBoxSeparator)
        t2_view.addSubview_(sep_lang)

        # ── Sprache / Language ──
        t2_y -= 28
        h_lang = NSTextField.labelWithString_(t("language"))
        h_lang.setFrame_(NSMakeRect(PAD, t2_y + 2, 150, 20))
        h_lang.setFont_(NSFont.boldSystemFontOfSize_(13))
        t2_view.addSubview_(h_lang)

        t2_y -= 30
        lang_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(PAD + 25, t2_y, 220, 24))
        lang_popup.removeAllItems()
        langs_ui = [
            ("Deutsch", "de"),
            ("English", "en"),
            ("Français", "fr"),
            ("Español", "es"),
            ("Português", "pt")
        ]
        saved_lang = self.settings.get("language", "de")
        _selected_lang_idx = 0
        for _ii, (_dn, _code) in enumerate(langs_ui):
            lang_popup.addItemWithTitle_(_dn)
            if _code == saved_lang:
                _selected_lang_idx = _ii
        lang_popup.selectItemAtIndex_(_selected_lang_idx)
        t2_view.addSubview_(lang_popup)
        controls["language"] = lang_popup

        # ── TAB 3: IMPORT-EINSTELLUNGEN ──────────────────────────────────
        tab3 = NSTabViewItem.alloc().initWithIdentifier_("Import")
        tab3.setLabel_(t("tab_import"))
        t3_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab3.setView_(t3_view)

        t3_y = t3_view.frame().size.height - 35

        h_imp = NSTextField.labelWithString_(t("lbl_import_settings_header"))
        h_imp.setFrame_(NSMakeRect(PAD, t3_y, 400, 20))
        h_imp.setFont_(NSFont.boldSystemFontOfSize_(13))
        t3_view.addSubview_(h_imp)

        t3_y -= 35
        cb_close = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, t3_y, 20, 20))
        cb_close.setButtonType_(AppKit.NSButtonTypeSwitch); cb_close.setTitle_("")
        cb_close.setState_(NSOnState if self.settings.get("import_close_session", True) else NSOffState)
        t3_view.addSubview_(cb_close); controls["import_close_session"] = cb_close
        l_close = NSTextField.labelWithString_(t("lbl_import_close_session"))
        l_close.setFrame_(NSMakeRect(PAD + 25, t3_y + 2, 450, 20)); t3_view.addSubview_(l_close)

        t3_y -= 30
        l_desc = NSTextField.labelWithString_(t("lbl_import_close_desc"))
        l_desc.setFrame_(NSMakeRect(PAD + 25, t3_y - 30, 450, 50))
        l_desc.setFont_(NSFont.systemFontOfSize_(11))
        l_desc.setTextColor_(NSColor.secondaryLabelColor())
        l_desc.setBezeled_(False); l_desc.setDrawsBackground_(False)
        l_desc.setEditable_(False); l_desc.setSelectable_(False)
        t3_view.addSubview_(l_desc)

        # ── Audio verschieben (Spur-Move) ─────────────────────────────────
        t3_y -= 78
        sep_mv = AppKit.NSBox.alloc().initWithFrame_(
            NSMakeRect(PAD, t3_y + 20, WIN_W - PAD * 2 - 40, 1))
        sep_mv.setBoxType_(AppKit.NSBoxSeparator)
        t3_view.addSubview_(sep_mv)

        h_mv = NSTextField.labelWithString_(t("lbl_move_audio"))
        h_mv.setFrame_(NSMakeRect(PAD, t3_y, 400, 20))
        h_mv.setFont_(NSFont.boldSystemFontOfSize_(13))
        t3_view.addSubview_(h_mv)

        t3_y -= 20
        d_mv = NSTextField.labelWithString_(t("lbl_move_hint"))
        d_mv.setFrame_(NSMakeRect(PAD, t3_y, WIN_W - PAD * 2 - 40, 18))
        d_mv.setFont_(NSFont.systemFontOfSize_(11))
        d_mv.setTextColor_(NSColor.secondaryLabelColor())
        t3_view.addSubview_(d_mv)

        # Spalten-Überschriften
        t3_y -= 26
        cs = NSTextField.labelWithString_(t("lbl_move_source"))
        cs.setFrame_(NSMakeRect(PAD, t3_y, 150, 18)); cs.setFont_(NSFont.boldSystemFontOfSize_(11))
        t3_view.addSubview_(cs)
        ct = NSTextField.labelWithString_(t("lbl_move_target"))
        ct.setFrame_(NSMakeRect(PAD + 200, t3_y, 150, 18)); ct.setFont_(NSFont.boldSystemFontOfSize_(11))
        t3_view.addSubview_(ct)

        # Zustand initialisieren (Paare aus den gespeicherten Listen)
        _ms = self.settings.get("move_audio_source_tracks", []) or []
        _mt = self.settings.get("move_audio_target_tracks", []) or []
        _n = max(len(_ms), len(_mt))
        self._move_pairs = [[_ms[i] if i < len(_ms) else "",
                             _mt[i] if i < len(_mt) else ""] for i in range(_n)]
        if not self._move_pairs:
            self._move_pairs = [["", ""]]
        self._move_track_options = [""] + list(track_names or [])
        self._move_t3_view = t3_view
        self._move_rows_top_y = t3_y - 30
        self._move_target = _MoveAudioTarget.alloc().init()
        self._move_target._app = self
        self._move_section_views = []
        self._render_move_section()

        # ── TAB 4: WEBTRIGGER ─────────────────────────────────────────────
        tab4 = NSTabViewItem.alloc().initWithIdentifier_("Webtrigger")
        tab4.setLabel_(t("tab_webtrigger"))
        t4_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab4.setView_(t4_view)

        t4_y = t4_view.frame().size.height - 35

        h_wt = NSTextField.labelWithString_(t("lbl_http_server_config"))
        h_wt.setFrame_(NSMakeRect(PAD, t4_y, 400, 20))
        h_wt.setFont_(NSFont.boldSystemFontOfSize_(13))
        t4_view.addSubview_(h_wt)

        t4_y -= 30
        l_port = NSTextField.labelWithString_(t("lbl_http_port"))
        l_port.setFrame_(NSMakeRect(PAD, t4_y, 40, 22)); t4_view.addSubview_(l_port)
        tf_port = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 45, t4_y, 70, 22))
        tf_port.setStringValue_(str(self.settings.get("http_port", 8899)))
        t4_view.addSubview_(tf_port); controls["http_port"] = tf_port

        l_port_info = NSTextField.labelWithString_(t("lbl_http_restart_info"))
        l_port_info.setFrame_(NSMakeRect(PAD + 125, t4_y + 2, 280, 18))
        l_port_info.setFont_(NSFont.systemFontOfSize_(11))
        l_port_info.setTextColor_(NSColor.secondaryLabelColor())
        t4_view.addSubview_(l_port_info)

        # ── Netzwerk-Interface ──────────────────────────────────────────
        t4_y -= 30
        sep_iface = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t4_y, WIN_W - PAD * 2 - 40, 1))
        sep_iface.setBoxType_(AppKit.NSBoxSeparator)
        t4_view.addSubview_(sep_iface)

        t4_y -= 28
        h_iface = NSTextField.labelWithString_(t("lbl_network_interface"))
        h_iface.setFrame_(NSMakeRect(PAD, t4_y, 300, 20))
        h_iface.setFont_(NSFont.boldSystemFontOfSize_(13))
        t4_view.addSubview_(h_iface)

        t4_y -= 30
        l_iface = NSTextField.labelWithString_(t("lbl_interface"))
        l_iface.setFrame_(NSMakeRect(PAD, t4_y + 2, 80, 20))
        l_iface.setFont_(NSFont.systemFontOfSize_(12))
        t4_view.addSubview_(l_iface)

        _iface_list = _get_network_interfaces()
        saved_host = self.settings.get("http_bind_host", "127.0.0.1")
        iface_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(PAD + 85, t4_y, 260, 24))
        iface_popup.removeAllItems()
        _selected_iface_idx = 0
        for _ii, (_dn, _ip) in enumerate(_iface_list):
            iface_popup.addItemWithTitle_(_dn)
            if _ip == saved_host:
                _selected_iface_idx = _ii
        iface_popup.selectItemAtIndex_(_selected_iface_idx)
        t4_view.addSubview_(iface_popup)
        controls["http_bind_host"] = iface_popup

        l_iface_note = NSTextField.labelWithString_(t("lbl_http_restart_info"))
        l_iface_note.setFrame_(NSMakeRect(PAD + 355, t4_y + 2, 240, 18))
        l_iface_note.setFont_(NSFont.systemFontOfSize_(11))
        l_iface_note.setTextColor_(NSColor.secondaryLabelColor())
        t4_view.addSubview_(l_iface_note)

        # ── Token (Webtrigger-Authentifizierung) ────────────────────────
        t4_y -= 30
        l_token = NSTextField.labelWithString_(t("lbl_webtrigger_token"))
        l_token.setFrame_(NSMakeRect(PAD, t4_y + 2, 80, 20))
        l_token.setFont_(NSFont.systemFontOfSize_(12))
        t4_view.addSubview_(l_token)

        tf_token = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 85, t4_y, 260, 22))
        tf_token.setStringValue_(self.settings.get("webtrigger_token", "") or "")
        tf_token.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
        t4_view.addSubview_(tf_token)
        controls["webtrigger_token"] = tf_token

        btn_gen_token = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 355, t4_y, 90, 22))
        btn_gen_token.setTitle_(t("btn_generate_token"))
        btn_gen_token.setBezelStyle_(NSBezelStyleRounded)
        btn_gen_token.setFont_(NSFont.systemFontOfSize_(11))
        t4_view.addSubview_(btn_gen_token)

        btn_clear_token = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 450, t4_y, 90, 22))
        btn_clear_token.setTitle_(t("btn_clear_token"))
        btn_clear_token.setBezelStyle_(NSBezelStyleRounded)
        btn_clear_token.setFont_(NSFont.systemFontOfSize_(11))
        t4_view.addSubview_(btn_clear_token)

        t4_y -= 20
        l_token_hint = NSTextField.labelWithString_(t("lbl_webtrigger_token_hint"))
        l_token_hint.setFrame_(NSMakeRect(PAD + 85, t4_y, 460, 18))
        l_token_hint.setFont_(NSFont.systemFontOfSize_(11))
        l_token_hint.setTextColor_(NSColor.secondaryLabelColor())
        t4_view.addSubview_(l_token_hint)
        self._token_field = tf_token
        self._token_gen_button = btn_gen_token
        self._token_clear_button = btn_clear_token

        t4_y -= 35
        h_urls = NSTextField.labelWithString_(t("lbl_trigger_urls"))
        h_urls.setFrame_(NSMakeRect(PAD, t4_y, 400, 20))
        h_urls.setFont_(NSFont.boldSystemFontOfSize_(13))
        t4_view.addSubview_(h_urls)

        port = getattr(self, '_http_port', self.settings.get("http_port", 8899))
        display_host = getattr(self, '_http_bind_host_display',
                               self.settings.get("http_bind_host", "127.0.0.1"))
        _tok = self.settings.get("webtrigger_token", "") or ""
        _token_qs = f"?token={_tok}" if _tok else ""

        self._webtrigger_urls = []

        _core_entries = [
            ("/trigger",           "Record A"),
            ("/trigger2",          "Record B"),
            ("/play",              "Play Input/Stop (Toggle)"),
            ("/play_custom",       "Play Custom (KH2/ST Abh)"),
            ("/start",             "Cursor → Start-Timecode"),
            ("/move_audio",        t("lbl_move_audio")),
            ("/export_wav",            "WAV Export"),
            ("/export_aaf_embedded",   "AAF Export (Embedded)"),
            ("/export_aaf_reference",  "AAF Export (Reference)"),
            ("/export_interplay",      "Interplay Export"),
            ("/import",            "Interplay Import"),
        ]

        for path, label in _core_entries:
            t4_y -= 28
            url = f"http://{display_host}:{port}{path}{_token_qs}"
            self._webtrigger_urls.append((url, label))
            tag_idx = len(self._webtrigger_urls) - 1

            l_url = NSTextField.labelWithString_(f"{label}:")
            l_url.setFrame_(NSMakeRect(PAD, t4_y + 2, 140, 18))
            l_url.setFont_(NSFont.systemFontOfSize_(12))
            t4_view.addSubview_(l_url)

            tf_url = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 145, t4_y, 405, 22))
            tf_url.setStringValue_(url)
            tf_url.setEditable_(False); tf_url.setSelectable_(True)
            tf_url.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
            t4_view.addSubview_(tf_url)

            copy_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 560, t4_y, 70, 22))
            copy_btn.setTitle_(t("btn_copy"))
            copy_btn.setBezelStyle_(NSBezelStyleRounded)
            copy_btn.setFont_(NSFont.systemFontOfSize_(11))
            copy_btn.setTag_(tag_idx)
            t4_view.addSubview_(copy_btn)

        # ── Preset Webtrigger URLs ────────────────────────────────────────
        t4_y -= 15
        sep_presets = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t4_y, WIN_W - PAD * 2 - 40, 1))
        sep_presets.setBoxType_(AppKit.NSBoxSeparator)
        t4_view.addSubview_(sep_presets)

        t4_y -= 25
        h_preset_urls = NSTextField.labelWithString_(t("lbl_preset_trigger_urls"))
        h_preset_urls.setFrame_(NSMakeRect(PAD, t4_y, 400, 20))
        h_preset_urls.setFont_(NSFont.boldSystemFontOfSize_(13))
        t4_view.addSubview_(h_preset_urls)

        presets = self.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        for idx in range(8):
            preset_name = f"Preset {idx+1}"
            if idx < len(presets):
                preset_name = presets[idx].get("name", preset_name)

            path = f"/preset/{idx+1}"
            url = f"http://{display_host}:{port}{path}{_token_qs}"
            self._webtrigger_urls.append((url, preset_name))
            tag_idx = len(self._webtrigger_urls) - 1

            t4_y -= 28
            l_url = NSTextField.labelWithString_(f"{preset_name}:")
            l_url.setFrame_(NSMakeRect(PAD, t4_y + 2, 140, 18))
            l_url.setFont_(NSFont.systemFontOfSize_(12))
            t4_view.addSubview_(l_url)

            tf_url = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 145, t4_y, 405, 22))
            tf_url.setStringValue_(url)
            tf_url.setEditable_(False); tf_url.setSelectable_(True)
            tf_url.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
            t4_view.addSubview_(tf_url)

            copy_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 560, t4_y, 70, 22))
            copy_btn.setTitle_(t("btn_copy"))
            copy_btn.setBezelStyle_(NSBezelStyleRounded)
            copy_btn.setFont_(NSFont.systemFontOfSize_(11))
            copy_btn.setTag_(tag_idx)
            t4_view.addSubview_(copy_btn)

        self._webtrigger_buttons = []

        _wt_target = _WebtriggerCopyTarget.alloc().init()
        _wt_target._app = self
        _wt_target._token_field = getattr(self, '_token_field', None)
        for sv in t4_view.subviews():
            if isinstance(sv, NSButton) and sv.title() == t("btn_copy"):
                sv.setTarget_(_wt_target)
                sv.setAction_(objc.selector(_wt_target.onCopy_, signature=b'v@:@'))
                self._webtrigger_buttons.append(sv)
        # Token-Buttons verdrahten
        if getattr(self, '_token_gen_button', None) is not None:
            self._token_gen_button.setTarget_(_wt_target)
            self._token_gen_button.setAction_(
                objc.selector(_wt_target.onGenerateToken_, signature=b'v@:@'))
        if getattr(self, '_token_clear_button', None) is not None:
            self._token_clear_button.setTarget_(_wt_target)
            self._token_clear_button.setAction_(
                objc.selector(_wt_target.onClearToken_, signature=b'v@:@'))
        self._wt_target = _wt_target

        # ── TAB 5: PRESETS ──────────────────────────────────────────────────
        tab5 = NSTabViewItem.alloc().initWithIdentifier_("Presets")
        tab5.setLabel_(t("tab_presets"))
        t5_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab5.setView_(t5_view)

        t5_y = t5_view.frame().size.height - 35

        h_pr = NSTextField.labelWithString_(t("tab_presets"))
        h_pr.setFrame_(NSMakeRect(PAD, t5_y, 400, 20))
        h_pr.setFont_(NSFont.boldSystemFontOfSize_(13))
        t5_view.addSubview_(h_pr)

        t5_y -= 12
        sep_pr0 = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t5_y, WIN_W - PAD * 2 - 40, 1))
        sep_pr0.setBoxType_(AppKit.NSBoxSeparator)
        t5_view.addSubview_(sep_pr0)

        t5_y -= 35
        l_psel = NSTextField.labelWithString_(t("preset_select"))
        l_psel.setFrame_(NSMakeRect(PAD, t5_y + 3, 130, 20))
        l_psel.setFont_(NSFont.systemFontOfSize_(12))
        t5_view.addSubview_(l_psel)

        presets_tab_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(PAD + 135, t5_y, 220, 24))
        presets_tab_popup.removeAllItems()
        _cur_presets = self.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        for _pp in _cur_presets:
            presets_tab_popup.addItemWithTitle_(_pp.get("name", "Preset"))
        t5_view.addSubview_(presets_tab_popup)

        t5_y -= 40
        btn_load_pr  = NSButton.alloc().initWithFrame_(NSMakeRect(PAD,        t5_y, 75, 28))
        btn_load_pr.setTitle_(t("btn_load"));      btn_load_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_load_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_load_pr)

        btn_save_pr  = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 85,   t5_y, 90, 28))
        btn_save_pr.setTitle_(t("save"));  btn_save_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_save_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_save_pr)

        btn_rename_pr = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 185, t5_y, 105, 28))
        btn_rename_pr.setTitle_(t("btn_rename")); btn_rename_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_rename_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_rename_pr)

        btn_new_pr   = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 300,  t5_y, 60, 28))
        btn_new_pr.setTitle_(t("btn_new"));         btn_new_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_new_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_new_pr)

        btn_del_pr   = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 370,  t5_y, 80, 28))
        btn_del_pr.setTitle_(t("btn_delete"));     btn_del_pr.setBezelStyle_(NSBezelStyleRounded)
        btn_del_pr.setFont_(NSFont.systemFontOfSize_(12)); t5_view.addSubview_(btn_del_pr)

        t5_y -= 35
        sep_pr2 = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t5_y, WIN_W - PAD * 2 - 40, 1))
        sep_pr2.setBoxType_(AppKit.NSBoxSeparator)
        t5_view.addSubview_(sep_pr2)

        t5_y -= 28
        h_pr_info = NSTextField.labelWithString_(t("preset_contains"))
        h_pr_info.setFrame_(NSMakeRect(PAD, t5_y, 300, 20))
        h_pr_info.setFont_(NSFont.boldSystemFontOfSize_(12))
        t5_view.addSubview_(h_pr_info)

        _info_text = (
            t("preset_info_tracks") +
            t("preset_info_mon") +
            t("preset_info_export") +
            t("preset_info_keywords") +
            t("preset_info_import") +
            t("preset_info_webtrigger")
        )
        t5_y -= 95
        l_info = NSTextField.labelWithString_(_info_text)
        l_info.setFrame_(NSMakeRect(PAD + 15, t5_y, WIN_W - PAD * 2 - 55, 90))
        l_info.setFont_(NSFont.systemFontOfSize_(11))
        l_info.setTextColor_(NSColor.secondaryLabelColor())
        l_info.setBezeled_(False); l_info.setDrawsBackground_(False)
        l_info.setEditable_(False); l_info.setSelectable_(False)
        t5_view.addSubview_(l_info)

        # Tabs hinzufuegen
        # ── TAB 6: MONITORING ────────────────────────────────────────────
        tab6 = NSTabViewItem.alloc().initWithIdentifier_("Monitoring")
        tab6.setLabel_(t("tab_monitoring"))
        t6_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab6.setView_(t6_view)

        t6_y = t6_view.frame().size.height - 20

        lbl_mon_hdr = NSTextField.labelWithString_(t("mon_hdr"))
        lbl_mon_hdr.setFrame_(NSMakeRect(PAD, t6_y, 400, 20))
        lbl_mon_hdr.setFont_(NSFont.boldSystemFontOfSize_(13))
        t6_view.addSubview_(lbl_mon_hdr)

        t6_y -= 20
        lbl_mon_desc = NSTextField.labelWithString_(t("mon_desc"))
        lbl_mon_desc.setFrame_(NSMakeRect(PAD, t6_y, WIN_W - PAD * 2 - 40, 18))
        lbl_mon_desc.setFont_(NSFont.systemFontOfSize_(11))
        lbl_mon_desc.setTextColor_(NSColor.secondaryLabelColor())
        t6_view.addSubview_(lbl_mon_desc)

        t6_y -= 30

        # Spalten-Header
        for txt, x, w in [(t("mon_col_track"), PAD, 180), (t("mon_col_mute_start"), PAD + 190, 140), (t("mon_col_mute_stop"), PAD + 340, 120)]:
            h = NSTextField.labelWithString_(txt)
            h.setFrame_(NSMakeRect(x, t6_y, w, 18))
            h.setFont_(NSFont.boldSystemFontOfSize_(11))
            t6_view.addSubview_(h)

        track_options = [""] + (track_names or [])

        for ch_idx in range(1, 3):
            t6_y -= 35
            lbl_ch = NSTextField.labelWithString_(f"{t('mon_channel')} {ch_idx}:")
            lbl_ch.setFrame_(NSMakeRect(PAD, t6_y + 3, 55, 18))
            t6_view.addSubview_(lbl_ch)

            track_key    = f"play_custom_ch{ch_idx}_track"
            mute_s_key   = f"play_custom_ch{ch_idx}_mute_start"
            mute_end_key = f"play_custom_ch{ch_idx}_mute_stop"

            cur_track     = self.settings.get(track_key, DEFAULT_SETTINGS.get(track_key, ""))
            cur_mute_s    = self.settings.get(mute_s_key, DEFAULT_SETTINGS.get(mute_s_key, True))
            cur_mute_end  = self.settings.get(mute_end_key, DEFAULT_SETTINGS.get(mute_end_key, False))

            popup = AppKit.NSPopUpButton.alloc().initWithFrame_(NSMakeRect(PAD + 60, t6_y, 120, 24))
            popup.removeAllItems()
            for opt in track_options:
                popup.addItemWithTitle_(opt)
            if cur_track in track_options:
                popup.selectItemWithTitle_(cur_track)
            t6_view.addSubview_(popup)
            controls[track_key] = popup

            cb_start = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 190 + 40, t6_y + 2, 20, 20))
            cb_start.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb_start.setTitle_("")
            cb_start.setState_(AppKit.NSOnState if cur_mute_s else AppKit.NSOffState)
            t6_view.addSubview_(cb_start)
            controls[mute_s_key] = cb_start

            cb_stop = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 340 + 20, t6_y + 2, 20, 20))
            cb_stop.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb_stop.setTitle_("")
            cb_stop.setState_(AppKit.NSOnState if cur_mute_end else AppKit.NSOffState)
            t6_view.addSubview_(cb_stop)
            controls[mute_end_key] = cb_stop

        # ── TAB 7: VOCASTER / STREAM DECK ───────────────────────────────────
        # Immer vorhanden – die Stream-Deck-Sektion ist unabhängig vom Vocaster.
        tab7 = NSTabViewItem.alloc().initWithIdentifier_("VocasterStreamdeck")
        tab7.setLabel_("Vocaster / Stream Deck")
        t7_view = NSView.alloc().initWithFrame_(tab_view.contentRect())
        tab7.setView_(t7_view)
        t7_y = t7_view.frame().size.height - 35

        content_w = WIN_W - PAD * 2 - 40  # Verfügbare Breite

        # Hilfsfunktion: mehrzeiliges Label mit aktivierter Word-Wrap.
        def _wrap_label(text):
            lbl = NSTextField.labelWithString_(text)
            lbl.setFont_(NSFont.systemFontOfSize_(11))
            lbl.setTextColor_(NSColor.secondaryLabelColor())
            cell = lbl.cell()
            if hasattr(cell, "setWraps_"):
                cell.setWraps_(True)
            if hasattr(cell, "setLineBreakMode_"):
                cell.setLineBreakMode_(AppKit.NSLineBreakByWordWrapping)
            return lbl

        if getattr(self, "vocaster", None) is not None and self._vocaster_info:
            voc_info = self._vocaster_info
            voc_channels = voc_info.get("channels", 1)

            # Erkanntes Gerät
            l_dev = NSTextField.labelWithString_(
                f"{t('lbl_vocaster_device')}  {voc_info.get('name', 'Vocaster')}")
            l_dev.setFrame_(NSMakeRect(PAD, t7_y, WIN_W - PAD * 2 - 40, 20))
            l_dev.setFont_(NSFont.boldSystemFontOfSize_(13))
            t7_view.addSubview_(l_dev)

            # 48V beim Start – Checkbox
            t7_y -= 34
            cb_phantom = NSButton.alloc().initWithFrame_(
                NSMakeRect(PAD, t7_y, WIN_W - PAD * 2 - 40, 22))
            cb_phantom.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb_phantom.setTitle_(t("lbl_vocaster_phantom_start"))
            cb_phantom.setState_(
                AppKit.NSOnState if self.settings.get("vocaster_phantom_on_start", False)
                else AppKit.NSOffState)
            t7_view.addSubview_(cb_phantom)
            controls["vocaster_phantom_on_start"] = cb_phantom

            t7_y -= 20
            l_ph_hint = NSTextField.labelWithString_(t("lbl_vocaster_phantom_hint"))
            l_ph_hint.setFrame_(NSMakeRect(PAD + 22, t7_y, WIN_W - PAD * 2 - 60, 18))
            l_ph_hint.setFont_(NSFont.systemFontOfSize_(11))
            l_ph_hint.setTextColor_(NSColor.secondaryLabelColor())
            t7_view.addSubview_(l_ph_hint)

            # Webtrigger-URLs
            t7_y -= 22
            sep_v = AppKit.NSBox.alloc().initWithFrame_(
                NSMakeRect(PAD, t7_y, WIN_W - PAD * 2 - 40, 1))
            sep_v.setBoxType_(AppKit.NSBoxSeparator)
            t7_view.addSubview_(sep_v)

            t7_y -= 26
            h_vurls = NSTextField.labelWithString_(t("lbl_vocaster_autogain_urls"))
            h_vurls.setFrame_(NSMakeRect(PAD, t7_y, 400, 20))
            h_vurls.setFont_(NSFont.boldSystemFontOfSize_(13))
            t7_view.addSubview_(h_vurls)

            _voc_entries = [("/vocaster/autogain/host", t("lbl_vocaster_autogain_host"))]
            if voc_channels >= 2:
                _voc_entries.append(("/vocaster/autogain/guest", t("lbl_vocaster_autogain_guest")))
            _voc_entries.append(("/vocaster/phantom/on",  t("lbl_vocaster_phantom_on")))
            _voc_entries.append(("/vocaster/phantom/off", t("lbl_vocaster_phantom_off")))

            for vpath, vlabel in _voc_entries:
                t7_y -= 28
                vurl = f"http://{display_host}:{port}{vpath}{_token_qs}"
                self._webtrigger_urls.append((vurl, vlabel))
                v_tag = len(self._webtrigger_urls) - 1

                l_vu = NSTextField.labelWithString_(f"{vlabel}:")
                l_vu.setFrame_(NSMakeRect(PAD, t7_y + 2, 140, 18))
                l_vu.setFont_(NSFont.systemFontOfSize_(12))
                t7_view.addSubview_(l_vu)

                tf_vu = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 145, t7_y, 405, 22))
                tf_vu.setStringValue_(vurl)
                tf_vu.setEditable_(False); tf_vu.setSelectable_(True)
                tf_vu.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
                t7_view.addSubview_(tf_vu)

                cp_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 560, t7_y, 70, 22))
                cp_btn.setTitle_(t("btn_copy"))
                cp_btn.setBezelStyle_(NSBezelStyleRounded)
                cp_btn.setFont_(NSFont.systemFontOfSize_(11))
                cp_btn.setTag_(v_tag)
                t7_view.addSubview_(cp_btn)

            # ── Routing-Sektion (Hub-Ersatz) ────────────────────────────────
            t7_y -= 24
            sep_routing = AppKit.NSBox.alloc().initWithFrame_(
                NSMakeRect(PAD, t7_y, content_w, 1))
            sep_routing.setBoxType_(AppKit.NSBoxSeparator)
            t7_view.addSubview_(sep_routing)

            t7_y -= 26
            h_routing = NSTextField.labelWithString_(t("lbl_vocaster_routing_hdr"))
            h_routing.setFrame_(NSMakeRect(PAD, t7_y, 400, 20))
            h_routing.setFont_(NSFont.boldSystemFontOfSize_(13))
            t7_view.addSubview_(h_routing)

            # Checkbox „beim Start anwenden"
            t7_y -= 28
            cb_apply = NSButton.alloc().initWithFrame_(
                NSMakeRect(PAD, t7_y, content_w, 22))
            cb_apply.setButtonType_(AppKit.NSButtonTypeSwitch)
            cb_apply.setTitle_(t("lbl_vocaster_routing_apply_start"))
            cb_apply.setState_(
                AppKit.NSOnState if self.settings.get("vocaster_apply_routing_on_start", False)
                else AppKit.NSOffState)
            t7_view.addSubview_(cb_apply)
            controls["vocaster_apply_routing_on_start"] = cb_apply

            # Apply-Hint: zweizeilig → Höhe 30, mit Wrap
            t7_y -= 32
            l_apply_hint = _wrap_label(t("lbl_vocaster_routing_apply_hint"))
            l_apply_hint.setFrame_(NSMakeRect(PAD + 22, t7_y, content_w - 22, 30))
            t7_view.addSubview_(l_apply_hint)

            # Capture-Button (eigene Zeile, sauberer Abstand zum Hint oben)
            t7_y -= 32
            btn_capture = NSButton.alloc().initWithFrame_(
                NSMakeRect(PAD, t7_y, 220, 26))
            btn_capture.setTitle_(t("btn_vocaster_capture"))
            btn_capture.setBezelStyle_(NSBezelStyleRounded)
            t7_view.addSubview_(btn_capture)
            self._vocaster_capture_btn = btn_capture

            # Status-Label rechts neben dem Button – ausreichend Breite damit
            # "Letztes Capture: ..." vollständig sichtbar bleibt
            info = self.vocaster.saved_routing_info() if self.vocaster else None
            if info:
                status_text = t("lbl_vocaster_routing_saved").format(
                    when=info.get("saved_at", "?"),
                    model=info.get("model", "?"))
            else:
                status_text = t("lbl_vocaster_no_routing")
            l_capture_status = NSTextField.labelWithString_(status_text)
            l_capture_status.setFrame_(
                NSMakeRect(PAD + 232, t7_y + 4, content_w - 232, 18))
            l_capture_status.setFont_(NSFont.systemFontOfSize_(11))
            l_capture_status.setTextColor_(NSColor.secondaryLabelColor())
            t7_view.addSubview_(l_capture_status)
            self._vocaster_capture_status = l_capture_status

            # Workflow-Hinweis – ausreichend Abstand zum Button und ausreichende Höhe (3-zeilig)
            t7_y -= 46
            l_workflow = _wrap_label(t("lbl_vocaster_routing_workflow"))
            l_workflow.setFrame_(NSMakeRect(PAD, t7_y, content_w, 44))
            t7_view.addSubview_(l_workflow)

            # Hinweistext (allgemein, unten)
            t7_y -= 32
            l_vnote = _wrap_label(t("lbl_vocaster_note"))
            l_vnote.setFrame_(NSMakeRect(PAD, t7_y, content_w, 30))
            t7_view.addSubview_(l_vnote)

            # Copy-Buttons des Vocaster-Tabs mit bestehendem Target verdrahten
            for sv in t7_view.subviews():
                if isinstance(sv, NSButton) and sv.title() == t("btn_copy"):
                    sv.setTarget_(self._wt_target)
                    sv.setAction_(objc.selector(self._wt_target.onCopy_, signature=b'v@:@'))
                    self._webtrigger_buttons.append(sv)

            # Capture-Button-Action verdrahten (eigenes Target um Routing-Status zu refreshen)
            self._vocaster_capture_target = _VocasterCaptureTarget.alloc().init()
            self._vocaster_capture_target._app = self
            btn_capture.setTarget_(self._vocaster_capture_target)
            btn_capture.setAction_(objc.selector(
                self._vocaster_capture_target.onCapture_, signature=b'v@:@'))
        else:
            l_novoc = _wrap_label("Kein Vocaster verbunden – die Vocaster-Optionen "
                                  "erscheinen automatisch, sobald eines angeschlossen ist.")
            l_novoc.setFrame_(NSMakeRect(PAD, t7_y - 10, content_w, 30))
            t7_view.addSubview_(l_novoc)
            t7_y -= 44

        # ── Stream-Deck-Sektion (immer sichtbar) ────────────────────────────
        t7_y -= 14
        sep_sd = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(PAD, t7_y, content_w, 1))
        sep_sd.setBoxType_(AppKit.NSBoxSeparator)
        t7_view.addSubview_(sep_sd)

        t7_y -= 26
        h_sd = NSTextField.labelWithString_("Stream Deck")
        h_sd.setFrame_(NSMakeRect(PAD, t7_y, 400, 20))
        h_sd.setFont_(NSFont.boldSystemFontOfSize_(13))
        t7_view.addSubview_(h_sd)

        t7_y -= 36
        l_sd_note = _wrap_label("PunchBuddy bringt ein Stream-Deck-Plugin und Tasten-"
                                "Launcher mit. Die Steuerung läuft über einen lokalen "
                                "Socket – ganz ohne Netzwerk (kein Defender im Weg).")
        l_sd_note.setFrame_(NSMakeRect(PAD, t7_y, content_w, 32))
        t7_view.addSubview_(l_sd_note)

        t7_y -= 38
        btn_sd_install = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, t7_y, 200, 28))
        btn_sd_install.setTitle_("Plugin installieren")
        btn_sd_install.setBezelStyle_(NSBezelStyleRounded)
        t7_view.addSubview_(btn_sd_install)

        btn_sd_reveal = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 210, t7_y, 230, 28))
        btn_sd_reveal.setTitle_("Plugin speichern (Downloads)")
        btn_sd_reveal.setBezelStyle_(NSBezelStyleRounded)
        t7_view.addSubview_(btn_sd_reveal)

        t7_y -= 36
        btn_sd_launchers = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, t7_y, 260, 28))
        btn_sd_launchers.setTitle_("Tasten-Launcher erzeugen")
        btn_sd_launchers.setBezelStyle_(NSBezelStyleRounded)
        t7_view.addSubview_(btn_sd_launchers)

        t7_y -= 34
        sock_disp = self.settings.get("unix_socket_path", DEFAULT_UNIX_SOCKET) or DEFAULT_UNIX_SOCKET
        l_sd_sock = _wrap_label(f"Socket: {sock_disp}   ·   Installieren öffnet die "
                                "mitgelieferte .streamDeckPlugin (im Stream Deck bestätigen). "
                                "Launcher = .apps für die Aktion ‚System → Öffnen‘.")
        l_sd_sock.setFrame_(NSMakeRect(PAD, t7_y, content_w, 32))
        t7_view.addSubview_(l_sd_sock)

        # Stream-Deck-Buttons verdrahten
        self._streamdeck_target = _StreamDeckTarget.alloc().init()
        self._streamdeck_target._app = self
        btn_sd_install.setTarget_(self._streamdeck_target)
        btn_sd_install.setAction_(objc.selector(
            self._streamdeck_target.onInstall_, signature=b'v@:@'))
        btn_sd_reveal.setTarget_(self._streamdeck_target)
        btn_sd_reveal.setAction_(objc.selector(
            self._streamdeck_target.onReveal_, signature=b'v@:@'))
        btn_sd_launchers.setTarget_(self._streamdeck_target)
        btn_sd_launchers.setAction_(objc.selector(
            self._streamdeck_target.onLaunchers_, signature=b'v@:@'))

        tab_view.addTabViewItem_(tab1)
        tab_view.addTabViewItem_(tab2)
        tab_view.addTabViewItem_(tab3)
        tab_view.addTabViewItem_(tab4)
        tab_view.addTabViewItem_(tab5)
        tab_view.addTabViewItem_(tab6)
        tab_view.addTabViewItem_(tab7)
        content.addSubview_(tab_view)

        # Target
        target = _UnifiedSettingsTarget.alloc().init()
        target.setup(self, window, checkboxes, track_names, controls, presets_tab_popup,
                     iface_list=_iface_list)
        btn_load_pr.setTarget_(target)
        btn_load_pr.setAction_(objc.selector(target.onLoadPreset_, signature=b'v@:@'))
        btn_save_pr.setTarget_(target)
        btn_save_pr.setAction_(objc.selector(target.onSavePreset_, signature=b'v@:@'))
        btn_rename_pr.setTarget_(target)
        btn_rename_pr.setAction_(objc.selector(target.onRenamePreset_, signature=b'v@:@'))
        btn_new_pr.setTarget_(target)
        btn_new_pr.setAction_(objc.selector(target.onNewPreset_, signature=b'v@:@'))
        btn_del_pr.setTarget_(target)
        btn_del_pr.setAction_(objc.selector(target.onDeletePreset_, signature=b'v@:@'))

        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, 10, 100, 30))
        cancel_btn.setTitle_(t("cancel")); cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        cancel_btn.setTarget_(target)
        cancel_btn.setAction_(objc.selector(target.onCancel_, signature=b'v@:@'))
        content.addSubview_(cancel_btn)

        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(WIN_W - 120, 10, 100, 30))
        save_btn.setTitle_(t("save")); save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setKeyEquivalent_("\r"); save_btn.setTarget_(target)
        save_btn.setAction_(objc.selector(target.onSave_, signature=b'v@:@'))
        content.addSubview_(save_btn)

        global _config_refs
        _config_refs = [window, target, controls, checkboxes, tab_view,
                        presets_tab_popup, btn_load_pr, btn_save_pr, btn_rename_pr,
                        btn_new_pr, btn_del_pr, iface_popup, lang_popup]
        window.makeKeyAndOrderFront_(None)
        window.center()


    # ── Spuren-Konfiguration (natives Fenster) ────────────────────────────

    def _on_configure_tracks(self, _):
        """Liest Spuren aus Pro Tools via PTSL und öffnet Konfigurationsfenster."""
        _ensure_dock_icon()
        if not APPKIT_OK:
            rumps.alert("Fehler", "AppKit nicht verfügbar – natives Fenster kann nicht geöffnet werden.")
            return

        # Spuren aus Pro Tools lesen
        try:
            engine = _get_engine()
            if engine is None:
                rumps.alert("Fehler", "PTSL Engine nicht verfügbar.")
                return
            ok, all_tracks = _ptsl_call(engine.track_list, label="ConfigTrackList", timeout=5.0)
            if not ok or not all_tracks:
                rumps.alert("Fehler", "Konnte Spuren nicht aus Pro Tools lesen:\nTimeout")
                return
            track_names = [t.name for t in all_tracks]
        except Exception as e:
            rumps.alert("Fehler", f"Konnte Spuren nicht aus Pro Tools lesen:\n{e}")
            return

        if not track_names:
            rumps.alert("Keine Spuren", "Keine Spuren in der aktuellen Pro Tools Session gefunden.")
            return

        logging.info(f"Spuren aus Pro Tools gelesen: {track_names}")
        try:
            self._open_config_window(track_names)
        except Exception as e:
            logging.error(f"Fehler beim Oeffnen des Konfigurationsfensters: {e}", exc_info=True)
            rumps.alert("Fehler", f"Fenster konnte nicht geoeffnet werden:\n{e}")

    def _open_config_window(self, track_names):
        """Erstellt natives macOS Fenster mit Checkboxen für jede Spur."""
        if _close_existing_settings_window():
            return
        NSWindow          = AppKit.NSWindow
        NSView            = AppKit.NSView
        NSButton          = AppKit.NSButton
        NSTextField       = AppKit.NSTextField
        NSScrollView      = AppKit.NSScrollView
        NSFont            = AppKit.NSFont
        NSColor           = AppKit.NSColor
        NSMakeRect        = AppKit.NSMakeRect
        NSScreen          = AppKit.NSScreen
        NSOnState         = AppKit.NSOnState
        NSOffState        = AppKit.NSOffState
        NSBezelStyleRounded = AppKit.NSBezelStyleRounded

        # Aktuelle Settings laden
        rec_a_set = set(self.settings.get("tracks", []))
        mon_a_set = set(self.settings.get("monitor_tracks", []))
        rec_b_set = set(self.settings.get("tracks_b", []))
        mon_b_set = set(self.settings.get("monitor_tracks_b", []))
        export_set = set(self.settings.get("export_tracks", []))
        loud_set = set(self.settings.get("loudness_tracks", []))
        play_set = set(self.settings.get("play_monitor_tracks", []))

        # Layout-Konstanten
        ROW_H      = 28
        PAD        = 16
        LABEL_W    = 180
        CB_W       = 55
        GAP        = 20    # Abstand zwischen Trigger-Gruppen
        EXPORT_GAP = 20    # Abstand vor Export-Spalte
        LOUD_GAP   = 5     # Abstand vor Loud-Spalte
        PLAY_GAP   = 5     # Abstand vor Play-Spalte
        HEADER_H   = 60
        BUTTON_H   = 50
        WIN_W      = LABEL_W + CB_W * 4 + GAP + CB_W + EXPORT_GAP + CB_W + LOUD_GAP + CB_W + PLAY_GAP + PAD * 2 + 40

        scroll_h   = min(len(track_names) * ROW_H + PAD, 420)
        WIN_H      = HEADER_H + scroll_h + BUTTON_H + PAD

        # Fenster erstellen
        screen = NSScreen.mainScreen().frame()
        x = (screen.size.width - WIN_W) / 2
        y = (screen.size.height - WIN_H) / 2

        style = AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
        window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, WIN_W, WIN_H),
            style,
            AppKit.NSBackingStoreBuffered,
            False
        )
        window.setReleasedWhenClosed_(False)  # Lebenszeit über _config_refs steuern
        window.setTitle_("Einstellungen PunchBuddy – Spuren")
        window.setLevel_(3)  # Floating über anderen Fenstern

        # Fenster-Icon setzen
        _win_icon = _load_ns_icon()
        if _win_icon:
            _win_icon.setSize_(AppKit.NSMakeSize(32, 32))
            window.setRepresentedURL_(AppKit.NSURL.URLWithString_("punchbuddy://config"))
            window.standardWindowButton_(AppKit.NSWindowDocumentIconButton).setImage_(_win_icon)

        content = window.contentView()
        content.setWantsLayer_(True)

        # ── Header-Labels ─────────────────────────────────────────────────
        col_a_x = LABEL_W + PAD + 10
        col_b_x = col_a_x + CB_W * 2 + GAP
        col_export_x = col_b_x + CB_W * 2 + EXPORT_GAP
        col_loud_x   = col_export_x + CB_W + LOUD_GAP
        col_play_x   = col_loud_x + CB_W + PLAY_GAP

        # "Record A" Header
        lbl_a = NSTextField.labelWithString_("Record A")
        lbl_a.setFrame_(NSMakeRect(col_a_x, WIN_H - 30, CB_W * 2, 18))
        lbl_a.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_a.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_a)

        # "Record B" Header
        lbl_b = NSTextField.labelWithString_("Record B")
        lbl_b.setFrame_(NSMakeRect(col_b_x, WIN_H - 30, CB_W * 2, 18))
        lbl_b.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_b.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_b)

        # "Export" Header
        lbl_export = NSTextField.labelWithString_("Export")
        lbl_export.setFrame_(NSMakeRect(col_export_x, WIN_H - 30, CB_W, 18))
        lbl_export.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_export.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_export)

        # Sub-Header: Rec (rot) / Mon (grün) – farbige Labels statt dynamischer Hintergründe
        for base_x in [col_a_x, col_b_x]:
            sub_rec = NSTextField.labelWithString_("Rec")
            sub_rec.setFrame_(NSMakeRect(base_x, WIN_H - 48, CB_W, 16))
            sub_rec.setFont_(NSFont.boldSystemFontOfSize_(10))
            sub_rec.setTextColor_(NSColor.systemRedColor())
            sub_rec.setAlignment_(AppKit.NSTextAlignmentCenter)
            content.addSubview_(sub_rec)

            sub_mon = NSTextField.labelWithString_("Mon")
            sub_mon.setFrame_(NSMakeRect(base_x + CB_W, WIN_H - 48, CB_W, 16))
            sub_mon.setFont_(NSFont.boldSystemFontOfSize_(10))
            sub_mon.setTextColor_(NSColor.systemGreenColor())
            sub_mon.setAlignment_(AppKit.NSTextAlignmentCenter)
            content.addSubview_(sub_mon)

        # Export Sub-Header (blau)
        sub_export = NSTextField.labelWithString_("✔")
        sub_export.setFrame_(NSMakeRect(col_export_x, WIN_H - 48, CB_W, 16))
        sub_export.setFont_(NSFont.systemFontOfSize_(10))
        sub_export.setTextColor_(NSColor.systemBlueColor())
        sub_export.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(sub_export)

        # "Loudness" Header (orange)
        lbl_loud = NSTextField.labelWithString_("Loudness")
        lbl_loud.setFrame_(NSMakeRect(col_loud_x, WIN_H - 30, CB_W, 18))
        lbl_loud.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_loud.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_loud)

        sub_loud = NSTextField.labelWithString_("✔")
        sub_loud.setFrame_(NSMakeRect(col_loud_x, WIN_H - 48, CB_W, 16))
        sub_loud.setFont_(NSFont.systemFontOfSize_(10))
        sub_loud.setTextColor_(NSColor.systemOrangeColor())
        sub_loud.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(sub_loud)

        # "Play Input" Header
        lbl_play = NSTextField.labelWithString_("Play Input")
        lbl_play.setFrame_(NSMakeRect(col_play_x, WIN_H - 30, CB_W, 18))
        lbl_play.setFont_(NSFont.boldSystemFontOfSize_(12))
        lbl_play.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(lbl_play)

        sub_play = NSTextField.labelWithString_("Mon")
        sub_play.setFrame_(NSMakeRect(col_play_x, WIN_H - 48, CB_W, 16))
        sub_play.setFont_(NSFont.boldSystemFontOfSize_(10))
        sub_play.setTextColor_(NSColor.systemGreenColor())
        sub_play.setAlignment_(AppKit.NSTextAlignmentCenter)
        content.addSubview_(sub_play)

        # "Spur" Header
        lbl_spur = NSTextField.labelWithString_("Spur")
        lbl_spur.setFrame_(NSMakeRect(PAD, WIN_H - 30, LABEL_W, 18))
        lbl_spur.setFont_(NSFont.boldSystemFontOfSize_(12))
        content.addSubview_(lbl_spur)

        # ── ScrollView mit Track-Zeilen ───────────────────────────────────
        PRESET_H = 35  # Platz fuer Preset-Leiste
        scroll_frame = NSMakeRect(0, BUTTON_H, WIN_W, scroll_h)
        scroll_view = NSScrollView.alloc().initWithFrame_(scroll_frame)
        scroll_view.setHasVerticalScroller_(True)
        scroll_view.setAutohidesScrollers_(True)
        scroll_view.setBorderType_(AppKit.NSBezelBorder)

        doc_h = len(track_names) * ROW_H + PAD
        doc_view = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIN_W - 20, doc_h)
        )

        # Wir verwenden _ConfigButtonTarget als ObjC Action-Target
        target = _ConfigButtonTarget.alloc().init()
        
        checkboxes = {}  # {track_name: {rec_a, mon_a, rec_b, mon_b, export}}

        for i, name in enumerate(track_names):
            # In AppKit: y=0 ist unten, wir zaehlen von oben
            row_y = doc_h - (i + 1) * ROW_H

            # Track-Name Label
            lbl = NSTextField.labelWithString_(name)
            lbl.setFrame_(NSMakeRect(PAD, row_y + 4, LABEL_W - PAD, 20))
            lbl.setFont_(NSFont.systemFontOfSize_(12))
            doc_view.addSubview_(lbl)

            # Zebra-Streifen (abwechselnd) - NSBox statt layer/CGColor
            if i % 2 == 0:
                stripe = AppKit.NSBox.alloc().initWithFrame_(NSMakeRect(0, row_y, WIN_W - 20, ROW_H))
                stripe.setBoxType_(AppKit.NSBoxCustom)
                stripe.setBorderType_(AppKit.NSNoBorder)
                stripe.setFillColor_(NSColor.quaternaryLabelColor())
                doc_view.addSubview_positioned_relativeTo_(stripe, AppKit.NSWindowBelow, lbl)

            cbs = {}
            col_positions = [
                ("rec_a",    col_a_x,          rec_a_set),
                ("mon_a",    col_a_x + CB_W,   mon_a_set),
                ("rec_b",    col_b_x,          rec_b_set),
                ("mon_b",    col_b_x + CB_W,   mon_b_set),
                ("export",   col_export_x,      export_set),
                ("loud",     col_loud_x,        loud_set),
                ("play_mon", col_play_x,        play_set),
            ]

            for key, cx, active_set in col_positions:
                cb = NSButton.alloc().initWithFrame_(NSMakeRect(cx + 15, row_y + 3, 22, 22))
                cb.setButtonType_(AppKit.NSButtonTypeSwitch)
                cb.setTitle_("")
                is_on = name in active_set
                cb.setState_(NSOnState if is_on else NSOffState)
                cb.setIdentifier_(key)
                doc_view.addSubview_(cb)
                cbs[key] = cb

            checkboxes[name] = cbs

        scroll_view.setDocumentView_(doc_view)
        content.addSubview_(scroll_view)

        # ── Preset-Leiste (unterhalb der Buttons, ueber Scroll) ──────────
        preset_y = BUTTON_H + scroll_h + 2
        # Header-Bereich bleibt bestehen, Presets kommen zwischen Header und Scroll

        # Stattdessen: Presets am unteren Rand, zwischen ScrollView und Buttons
        # Verschiebe ScrollView nach oben und fuege Preset-Leiste darunter ein
        # → Einfacher: Preset-Leiste ins Fenster als eigene Zeile einbauen
        # Die Preset-Leiste kommt UNTERHALB der ScrollView, OBERHALB der Buttons.
        
        # Fenster vergroessern fuer Presets
        new_win_h = WIN_H + PRESET_H
        window.setFrame_display_(NSMakeRect(
            window.frame().origin.x, window.frame().origin.y - PRESET_H,
            WIN_W, new_win_h), True)
        
        # ScrollView nach oben verschieben
        sv_frame = scroll_view.frame()
        scroll_view.setFrame_(NSMakeRect(sv_frame.origin.x, sv_frame.origin.y + PRESET_H,
                                          sv_frame.size.width, sv_frame.size.height))
        
        # Header-Elemente nach oben verschieben
        for subview in list(content.subviews()):
            if subview != scroll_view:
                f = subview.frame()
                if f.origin.y > BUTTON_H + scroll_h - 5:  # Header-Bereich
                    subview.setFrame_(NSMakeRect(f.origin.x, f.origin.y + PRESET_H,
                                                  f.size.width, f.size.height))

        # Preset-Leiste zeichnen
        preset_base_y = BUTTON_H + 4
        presets = self.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])

        lbl_preset = NSTextField.labelWithString_("Presets:")
        lbl_preset.setFrame_(NSMakeRect(PAD, preset_base_y + 5, 55, 20))
        lbl_preset.setFont_(NSFont.boldSystemFontOfSize_(11))
        content.addSubview_(lbl_preset)

        # Popup-Button fuer Preset-Auswahl
        preset_popup = AppKit.NSPopUpButton.alloc().initWithFrame_(NSMakeRect(PAD + 55, preset_base_y + 3, 180, 24))
        preset_popup.removeAllItems()
        for p in presets:
            preset_popup.addItemWithTitle_(p.get("name", "Preset"))
        content.addSubview_(preset_popup)

        # Laden-Button
        load_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 240, preset_base_y + 3, 65, 24))
        load_btn.setTitle_("Laden")
        load_btn.setBezelStyle_(NSBezelStyleRounded)
        load_btn.setFont_(NSFont.systemFontOfSize_(11))
        content.addSubview_(load_btn)

        # Speichern-Button
        save_preset_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 310, preset_base_y + 3, 80, 24))
        save_preset_btn.setTitle_("Speichern")
        save_preset_btn.setBezelStyle_(NSBezelStyleRounded)
        save_preset_btn.setFont_(NSFont.systemFontOfSize_(11))
        content.addSubview_(save_preset_btn)

        # Umbenennen-Button
        rename_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD + 395, preset_base_y + 3, 90, 24))
        rename_btn.setTitle_("Umbenennen")
        rename_btn.setBezelStyle_(NSBezelStyleRounded)
        rename_btn.setFont_(NSFont.systemFontOfSize_(11))
        content.addSubview_(rename_btn)

        # ── Buttons ───────────────────────────────────────────────────────
        target.setup(self, checkboxes, track_names, window, preset_popup)

        # Preset-Button Actions setzen
        load_btn.setTarget_(target)
        load_btn.setAction_(objc.selector(target.onLoadPreset_, signature=b'v@:@'))
        save_preset_btn.setTarget_(target)
        save_preset_btn.setAction_(objc.selector(target.onSavePreset_, signature=b'v@:@'))
        rename_btn.setTarget_(target)
        rename_btn.setAction_(objc.selector(target.onRenamePreset_, signature=b'v@:@'))

        cancel_btn = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, 12, 100, 30))
        cancel_btn.setTitle_("Abbrechen")
        cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        cancel_btn.setTarget_(target)
        cancel_btn.setAction_(objc.selector(target.onCancel_, signature=b'v@:@'))
        content.addSubview_(cancel_btn)

        play_btn = NSButton.alloc().initWithFrame_(NSMakeRect(WIN_W // 2 - 60, 12, 120, 30))
        play_btn.setTitle_("Play / Stop Testen")
        play_btn.setBezelStyle_(NSBezelStyleRounded)
        play_btn.setTarget_(target)
        play_btn.setAction_(objc.selector(target.onPlay_, signature=b'v@:@'))
        content.addSubview_(play_btn)

        save_btn = NSButton.alloc().initWithFrame_(NSMakeRect(WIN_W - 120, 12, 100, 30))
        save_btn.setTitle_("Speichern")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setKeyEquivalent_("\r")  # Enter = Speichern
        save_btn.setTarget_(target)
        save_btn.setAction_(objc.selector(target.onSave_, signature=b'v@:@'))
        content.addSubview_(save_btn)

        # Referenzen in globaler Liste halten (NICHT als Instanz-Attribute!)
        # Python-Attribut-Assignments auf ObjC-Proxies loesen Deallokations-
        # kaskaden aus die in PyObjC/Python 3.14 crashen (SIGBUS/SIGSEGV).
        global _config_refs
        _config_refs = [window, target, checkboxes, doc_view, scroll_view, preset_popup,
                        load_btn, save_preset_btn, rename_btn, play_btn]
        window.makeKeyAndOrderFront_(None)
        window.center()


# ─────────────────────────────────────────────────────────────────────────────
# ObjC Helper-Klasse für Button-Actions im Konfigurationsfenster
# ─────────────────────────────────────────────────────────────────────────────
if APPKIT_OK:
    class _ConfigButtonTarget(AppKit.NSObject):
        """ObjC-kompatibles Target fuer Save/Cancel/Preset Buttons."""

        @objc.python_method
        def setup(self, app_ref, checkboxes, track_names, window, preset_popup=None):
            self._app = app_ref
            self._checkboxes = checkboxes
            self._track_names = track_names
            self._window = window
            self._preset_popup = preset_popup

        def onCheckboxToggle_(self, sender):
            pass

        @objc.python_method
        def _read_current_config(self):
            rec_a, mon_a, rec_b, mon_b, export_list, loud_list, play_list = [], [], [], [], [], [], []
            for name in self._track_names:
                cbs = self._checkboxes.get(name)
                if not cbs:
                    continue
                if cbs["rec_a"].state() == AppKit.NSOnState:
                    rec_a.append(name)
                if cbs["mon_a"].state() == AppKit.NSOnState:
                    mon_a.append(name)
                if cbs["rec_b"].state() == AppKit.NSOnState:
                    rec_b.append(name)
                if cbs["mon_b"].state() == AppKit.NSOnState:
                    mon_b.append(name)
                if cbs["export"].state() == AppKit.NSOnState:
                    export_list.append(name)
                if cbs["loud"].state() == AppKit.NSOnState:
                    loud_list.append(name)
                if cbs.get("play_mon") and cbs["play_mon"].state() == AppKit.NSOnState:
                    play_list.append(name)
            return rec_a, mon_a, rec_b, mon_b, export_list, loud_list, play_list

        @objc.python_method
        def _apply_config(self, rec_a, mon_a, rec_b, mon_b, export_list, loud_list=None, play_list=None):
            ra, ma, rb, mb, ex = set(rec_a), set(mon_a), set(rec_b), set(mon_b), set(export_list)
            lo = set(loud_list) if loud_list else set()
            pl = set(play_list) if play_list else set()
            for name in self._track_names:
                cbs = self._checkboxes.get(name)
                if not cbs:
                    continue
                cbs["rec_a"].setState_(AppKit.NSOnState if name in ra else AppKit.NSOffState)
                cbs["mon_a"].setState_(AppKit.NSOnState if name in ma else AppKit.NSOffState)
                cbs["rec_b"].setState_(AppKit.NSOnState if name in rb else AppKit.NSOffState)
                cbs["mon_b"].setState_(AppKit.NSOnState if name in mb else AppKit.NSOffState)
                cbs["export"].setState_(AppKit.NSOnState if name in ex else AppKit.NSOffState)
                cbs["loud"].setState_(AppKit.NSOnState if name in lo else AppKit.NSOffState)
                if "play_mon" in cbs:
                    cbs["play_mon"].setState_(AppKit.NSOnState if name in pl else AppKit.NSOffState)

        def onLoadPreset_(self, sender):
            idx = self._preset_popup.indexOfSelectedItem()
            presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
            if 0 <= idx < len(presets):
                p = presets[idx]
                self._apply_config(
                    p.get("rec_a", []), p.get("mon_a", []),
                    p.get("rec_b", []), p.get("mon_b", []),
                    p.get("export", [])
                )
                logging.info(f"Preset {p.get('name')} geladen.")

        def onSavePreset_(self, sender):
            idx = self._preset_popup.indexOfSelectedItem()
            presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
            if 0 <= idx < len(presets):
                rec_a, mon_a, rec_b, mon_b, export_list, loud_list, play_list = self._read_current_config()
                presets[idx]["rec_a"] = rec_a
                presets[idx]["mon_a"] = mon_a
                presets[idx]["rec_b"] = rec_b
                presets[idx]["mon_b"] = mon_b
                presets[idx]["export"] = export_list
                presets[idx]["loudness_tracks"] = loud_list
                self._app.settings["track_presets"] = presets
                save_settings(self._app.settings)
                name = presets[idx].get("name", f"Preset {idx+1}")
                logging.info(f"Preset {name} gespeichert.")
                rumps.alert("Preset gespeichert", f"{name} wurde gespeichert.")

        def onRenamePreset_(self, sender):
            idx = self._preset_popup.indexOfSelectedItem()
            presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
            if 0 <= idx < len(presets):
                old_name = presets[idx].get("name", f"Preset {idx+1}")
                response = rumps.Window(
                    title="Preset umbenennen",
                    message=f"Neuer Name fuer {old_name}:",
                    default_text=old_name,
                    ok="Umbenennen",
                    cancel="Abbrechen"
                ).run()
                if response.clicked:
                    new_name = response.text.strip()
                    if new_name:
                        presets[idx]["name"] = new_name
                        self._app.settings["track_presets"] = presets
                        save_settings(self._app.settings)
                        self._preset_popup.itemAtIndex_(idx).setTitle_(new_name)
                        logging.info(f"Preset umbenannt: {old_name} -> {new_name}")

        def onSave_(self, sender):
            rec_a, mon_a, rec_b, mon_b, export_list, loud_list, play_list = self._read_current_config()
            self._app.settings["tracks"] = rec_a
            self._app.settings["monitor_tracks"] = mon_a
            self._app.settings["tracks_b"] = rec_b
            self._app.settings["monitor_tracks_b"] = mon_b
            self._app.settings["export_tracks"] = export_list
            self._app.settings["loudness_tracks"] = loud_list
            self._app.settings["play_monitor_tracks"] = play_list
            save_settings(self._app.settings)
            self._window.close()
            logging.info(f"Spuren-Konfiguration gespeichert:")
            logging.info(f"  Trigger A: Rec={rec_a}, Mon={mon_a}")
            logging.info(f"  Trigger B: Rec={rec_b}, Mon={mon_b}")
            logging.info(f"  Export: {export_list}")
            logging.info(f"  Loudness: {loud_list}")
            logging.info(f"  Play Mon: {play_list}")
            rumps.alert("Gespeichert",
                f"Trigger A: {len(rec_a)} Rec, {len(mon_a)} Mon\n"
                f"Trigger B: {len(rec_b)} Rec, {len(mon_b)} Mon\n"
                f"Export: {len(export_list)} Spuren\n"
                f"Loudness: {len(loud_list)} Spuren\n"
                f"Play Mon: {len(play_list)} Spuren")

        def onCancel_(self, sender):
            self._window.close()



    class _SettingsButtonTarget(AppKit.NSObject):
        """ObjC-kompatibles Target für Settings Save/Cancel Buttons."""

        @objc.python_method
        def setup(self, app_ref, controls, window):
            self._app = app_ref
            self._controls = controls
            self._window = window

        def onSave_(self, sender):
            # Lese Werte aus den Textfeldern und Checkboxen
            try:
                l_enabled = (self._controls["loudness_enabled"].state() == AppKit.NSOnState)
                t_lufs = float(self._controls["target_lufs"].stringValue().replace(",", "."))
                m_tp = float(self._controls["max_truepeak"].stringValue().replace(",", "."))
                i_enabled = (self._controls["interplay_enabled"].state() == AppKit.NSOnState)
                i_ws = self._controls["interplay_workspace"].stringValue()
                i_steps = int(self._controls["interplay_workspace_steps"].stringValue())

                e_tc = self._controls["export_start_tc"].stringValue().strip()
                # TC Format validieren: HH:MM:SS:FF
                tc_parts = e_tc.split(":")
                if len(tc_parts) != 4 or not all(p.isdigit() for p in tc_parts):
                    raise ValueError(f"Ungültiger Timecode: '{e_tc}'\nFormat: HH:MM:SS:FF (z.B. 10:00:00:00)")

                if i_steps < 0:
                    raise ValueError("Steps muss >= 0 sein")

                w_enabled = (self._controls["wav_export_enabled"].state() == AppKit.NSOnState)
                a_enabled = (self._controls["aaf_export_enabled"].state() == AppKit.NSOnState)

                self._app.settings["export_start_tc"] = e_tc
                self._app.settings["wav_export_enabled"] = w_enabled
                self._app.settings["aaf_export_enabled"] = a_enabled
                self._app.settings["loudness_enabled"] = l_enabled
                self._app.settings["target_lufs"] = t_lufs
                self._app.settings["max_truepeak"] = m_tp
                self._app.settings["interplay_enabled"] = i_enabled
                self._app.settings["interplay_workspace"] = i_ws
                self._app.settings["interplay_workspace_steps"] = i_steps
                
                save_settings(self._app.settings)
                logging.info(f"Einstellungen gespeichert: TC={e_tc}, WAV={w_enabled}, AAF={a_enabled}, Loudness={l_enabled}, Interplay={i_enabled}")
                
                self._window.close()
                rumps.alert("Gespeichert", "Erweiterte Einstellungen wurden erfolgreich gespeichert.")
            except ValueError as e:
                AppKit.NSAlert.alertWithMessageText_defaultButton_alternateButton_otherButton_informativeTextWithFormat_(
                    "Eingabefehler", "OK", None, None, f"Bitte ueberpruefe die Eingaben:\n{e}"
                ).runModal()

        def onCancel_(self, sender):
            self._window.close()

class _MoveAudioTarget(AppKit.NSObject):
    """ObjC Target für die Dropdowns und +/−-Buttons der Audio-verschieben-
    Konfiguration (Import-Tab). Delegiert an Methoden der App-Instanz (_app)."""

    def onSrc_(self, sender):
        try:
            self._app._move_set(sender.tag(), 0, sender.titleOfSelectedItem() or "")
        except Exception as e:
            logging.error(f"MoveAudio onSrc: {e}")

    def onTgt_(self, sender):
        try:
            self._app._move_set(sender.tag(), 1, sender.titleOfSelectedItem() or "")
        except Exception as e:
            logging.error(f"MoveAudio onTgt: {e}")

    def onAdd_(self, sender):
        try:
            self._app._move_add()
        except Exception as e:
            logging.error(f"MoveAudio onAdd: {e}")

    def onRemove_(self, sender):
        try:
            self._app._move_remove(sender.tag())
        except Exception as e:
            logging.error(f"MoveAudio onRemove: {e}")


class _ExportPathBrowseTarget(AppKit.NSObject):
    """ObjC Target für die 'Durchsuchen…'-Buttons der Export-Pfade.
    Öffnet einen NSOpenPanel (nur Ordner) und schreibt den Pfad ins zugehörige Feld."""

    def onBrowse_(self, sender):
        try:
            tag = sender.tag()
            fields = getattr(self, '_fields', None)
            if not fields or not (0 <= tag < len(fields)):
                return
            panel = AppKit.NSOpenPanel.openPanel()
            panel.setCanChooseDirectories_(True)
            panel.setCanChooseFiles_(False)
            panel.setAllowsMultipleSelection_(False)
            panel.setPrompt_(t("btn_browse"))
            if panel.runModal() == AppKit.NSModalResponseOK:
                url = panel.URLs()[0]
                fields[tag].setStringValue_(url.path())
        except Exception as e:
            logging.error(f"Export-Pfad Browse: {e}")


class _WebtriggerCopyTarget(AppKit.NSObject):
    """ObjC Target für die Kopieren-Buttons im Webtrigger-Tab."""

    @objc.python_method
    def _do_copy(self, sender):
        tag = sender.tag()
        if hasattr(self, '_app') and hasattr(self._app, '_webtrigger_urls'):
            urls = self._app._webtrigger_urls
            if 0 <= tag < len(urls):
                url, label = urls[tag]
                try:
                    subprocess.run(["pbcopy"], input=url, universal_newlines=True)
                    rumps.notification("PunchBuddy", "Kopiert", f"{label}: {url}")
                except Exception:
                    pass

    def onCopy_(self, sender):
        self._do_copy(sender)

    def onGenerateToken_(self, sender):
        """Erzeugt ein neues Token und schreibt es ins Token-Feld."""
        import secrets
        field = getattr(self, '_token_field', None)
        if field is not None:
            field.setStringValue_(secrets.token_urlsafe(16))

    def onClearToken_(self, sender):
        """Leert das Token-Feld (Webtrigger danach ungeschützt)."""
        field = getattr(self, '_token_field', None)
        if field is not None:
            field.setStringValue_("")

class _VocasterCaptureTarget(AppKit.NSObject):
    """ObjC Target für den 'Aktuelles Routing speichern'-Button im Vocaster-Tab."""

    def onCapture_(self, sender):
        app = getattr(self, "_app", None)
        if app is None or app.vocaster is None:
            return
        # Capture in Hintergrund-Thread, UI-Status danach im Main-Thread aktualisieren
        def work():
            ok, msg = app.vocaster.capture_routing_now()
            # Status-Label aktualisieren – via PerformSelectorOnMainThread sicher
            def update_ui():
                title = "PunchBuddy – Vocaster"
                if ok:
                    rumps.notification(title, t("msg_routing_captured"), msg)
                    info = app.vocaster.saved_routing_info()
                    lbl = getattr(app, "_vocaster_capture_status", None)
                    if lbl is not None and info is not None:
                        lbl.setStringValue_(t("lbl_vocaster_routing_saved").format(
                            when=info.get("saved_at", "?"),
                            model=info.get("model", "?")))
                else:
                    rumps.notification(title,
                                       t("msg_routing_capture_failed").format(err=msg), "")
            # rumps.notification ist Thread-safe; setStringValue_ auf NSTextField auch
            # (Cocoa erlaubt setStringValue_ aus Threads). Aber sicherheitshalber:
            try:
                AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(update_ui)
            except Exception:
                update_ui()
        threading.Thread(target=work, daemon=True).start()


# Befehlsliste für die Tasten-Launcher (Name, Anzeige, Socket-Payload).
_STREAMDECK_LAUNCHER_CMDS = [
    ("PunchBuddy-RecordA",        "Record A",                "record_a"),
    ("PunchBuddy-RecordB",        "Record B",                "record_b"),
    ("PunchBuddy-Play",           "Play / Stop",             "play"),
    ("PunchBuddy-PlayCustom",     "Play Custom",             "play_custom"),
    ("PunchBuddy-GotoStart",      "Cursor an Start",         "goto_start"),
    ("PunchBuddy-MoveAudio",      "Audio verschieben",       "move_audio"),
    ("PunchBuddy-Import",         "Import",                  "import"),
    ("PunchBuddy-ExportWav",      "Export WAV",              "export_wav"),
    ("PunchBuddy-ExportAaf",      "Export AAF",              "export_aaf"),
    ("PunchBuddy-ExportAafRef",   "Export AAF Ref",          "export_aaf_reference"),
    ("PunchBuddy-ExportInterplay","Export Interplay",        "export_interplay"),
] + [(f"PunchBuddy-Preset{i}", f"Preset {i}", f"preset {i}") for i in range(1, 9)] + [
    ("PunchBuddy-VocAutogainHost", "Vocaster Autogain Host",  "vocaster_autogain_host"),
    ("PunchBuddy-VocAutogainGuest","Vocaster Autogain Guest", "vocaster_autogain_guest"),
    ("PunchBuddy-VocPhantomOn",    "Vocaster Phantom AN",     "vocaster_phantom_on"),
    ("PunchBuddy-VocPhantomOff",   "Vocaster Phantom AUS",    "vocaster_phantom_off"),
]


def _build_streamdeck_launchers(dest, sock):
    """Erzeugt pro Befehl ein winziges .app, das via `nc -U` den (konfigurierten)
    Unix-Socket bedient – netzwerkfrei. Gibt die Anzahl erzeugter Apps zurück."""
    import re
    os.makedirs(dest, exist_ok=True)
    for appname, label, payload in _STREAMDECK_LAUNCHER_CMDS:
        app = os.path.join(dest, appname + ".app")
        macos = os.path.join(app, "Contents", "MacOS")
        if os.path.isdir(app):
            shutil.rmtree(app, ignore_errors=True)
        os.makedirs(macos, exist_ok=True)
        slug = re.sub(r"[^a-z0-9-]", "", appname.lower())
        plist = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            f'  <key>CFBundleName</key><string>{appname}</string>\n'
            f'  <key>CFBundleDisplayName</key><string>{label}</string>\n'
            f'  <key>CFBundleIdentifier</key><string>com.punchbuddy.launcher.{slug}</string>\n'
            '  <key>CFBundleExecutable</key><string>launcher</string>\n'
            '  <key>CFBundlePackageType</key><string>APPL</string>\n'
            '  <key>CFBundleVersion</key><string>1.0</string>\n'
            '  <key>LSBackgroundOnly</key><true/>\n'
            '  <key>LSUIElement</key><true/>\n'
            '</dict></plist>\n'
        )
        with open(os.path.join(app, "Contents", "Info.plist"), "w") as f:
            f.write(plist)
        launcher = (
            "#!/bin/sh\n"
            f"printf '%s' '{payload}' | /usr/bin/nc -U '{sock}' >/dev/null 2>&1 || "
            "/usr/bin/osascript -e 'display notification "
            "\"PunchBuddy nicht erreichbar\" with title \"PunchBuddy\"' >/dev/null 2>&1\n"
        )
        lp = os.path.join(macos, "launcher")
        with open(lp, "w") as f:
            f.write(launcher)
        os.chmod(lp, 0o755)
    return len(_STREAMDECK_LAUNCHER_CMDS)


class _StreamDeckTarget(AppKit.NSObject):
    """ObjC-Target für die Stream-Deck-Buttons im Einstellungs-Tab."""

    def onInstall_(self, sender):
        app = getattr(self, "_app", None)
        if app is not None:
            app._install_streamdeck_plugin()

    def onReveal_(self, sender):
        app = getattr(self, "_app", None)
        if app is not None:
            app._reveal_streamdeck_plugin()

    def onLaunchers_(self, sender):
        app = getattr(self, "_app", None)
        if app is not None:
            app._generate_streamdeck_launchers()


class _UnifiedSettingsTarget(AppKit.NSObject):
    """ObjC-kompatibles Target fuer das kombinierte Einstellungsfenster."""

    @objc.python_method
    def setup(self, app_ref, window, checkboxes, track_names, controls, preset_popup,
              iface_list=None):
        self._app = app_ref
        self._window = window
        self._checkboxes = checkboxes
        self._track_names = track_names
        self._controls = controls
        self._preset_popup = preset_popup
        self._iface_list = iface_list or [("Localhost (127.0.0.1)", "127.0.0.1")]

    @objc.python_method
    def _read_current_config(self):
        rec_a, rec_b, export_list, loud_list, play_list = [], [], [], [], []
        for name in self._track_names:
            cbs = self._checkboxes.get(name)
            if not cbs:
                continue
            if cbs.get("rec_a") and cbs["rec_a"].state() == AppKit.NSOnState: rec_a.append(name)
            if cbs.get("rec_b") and cbs["rec_b"].state() == AppKit.NSOnState: rec_b.append(name)
            if cbs.get("export") and cbs["export"].state() == AppKit.NSOnState: export_list.append(name)
            if cbs.get("loud") and cbs["loud"].state() == AppKit.NSOnState: loud_list.append(name)
            if cbs.get("play_mon") and cbs["play_mon"].state() == AppKit.NSOnState: play_list.append(name)
        return rec_a, rec_b, export_list, loud_list, play_list

    @objc.python_method
    def _apply_config(self, rec_a, rec_b, export_list, loud_list=None, play_list=None):
        ra, rb, ex = set(rec_a), set(rec_b), set(export_list)
        lo = set(loud_list) if loud_list else set()
        pl = set(play_list) if play_list else set()
        for name in self._track_names:
            cbs = self._checkboxes.get(name)
            if not cbs:
                continue
            if cbs.get("rec_a"):
                cbs["rec_a"].setState_(AppKit.NSOnState if name in ra else AppKit.NSOffState)
            if cbs.get("rec_b"):
                cbs["rec_b"].setState_(AppKit.NSOnState if name in rb else AppKit.NSOffState)
            if cbs.get("export"):
                cbs["export"].setState_(AppKit.NSOnState if name in ex else AppKit.NSOffState)
            if cbs.get("loud"):
                cbs["loud"].setState_(AppKit.NSOnState if name in lo else AppKit.NSOffState)
            if cbs.get("play_mon"):
                cbs["play_mon"].setState_(AppKit.NSOnState if name in pl else AppKit.NSOffState)

    @objc.python_method
    def _read_all_controls(self):
        """Returns a dict of all current control values (for preset saving)."""
        d = {}
        bool_keys = ["wav_export_enabled", "aaf_export_enabled", "interplay_enabled",
                     "loudness_enabled", "interplay_rename_enabled", "import_close_session"]
        for key in bool_keys:
            if key in self._controls:
                d[key] = (self._controls[key].state() == AppKit.NSOnState)
        str_keys = ["export_start_tc", "video_track", "interplay_workspace",
                    "export_error_keywords", "export_success_keywords",
                    "interplay_rename_prefix", "interplay_rename_suffix"]
        for key in str_keys:
            if key in self._controls:
                d[key] = self._controls[key].stringValue()
        int_keys = ["extend_count", "interplay_workspace_steps",
                    "interplay_rename_trim_start", "interplay_rename_trim_end", "http_port"]
        for key in int_keys:
            if key in self._controls:
                try:
                    d[key] = int(self._controls[key].stringValue())
                except ValueError:
                    pass
        float_keys = ["target_lufs", "max_truepeak"]
        for key in float_keys:
            if key in self._controls:
                try:
                    d[key] = float(self._controls[key].stringValue().replace(",", "."))
                except ValueError:
                    pass
        if "http_bind_host" in self._controls:
            idx_iface = self._controls["http_bind_host"].indexOfSelectedItem()
            if 0 <= idx_iface < len(self._iface_list):
                d["http_bind_host"] = self._iface_list[idx_iface][1]
        return d

    @objc.python_method
    def _apply_all_controls(self, p):
        """Applies preset dict p to all UI controls."""
        bool_keys = ["wav_export_enabled", "aaf_export_enabled", "interplay_enabled",
                     "loudness_enabled", "interplay_rename_enabled", "import_close_session"]
        for key in bool_keys:
            if key in self._controls and key in p:
                self._controls[key].setState_(
                    AppKit.NSOnState if p[key] else AppKit.NSOffState)
        str_keys = ["export_start_tc", "video_track", "interplay_workspace",
                    "export_error_keywords", "export_success_keywords",
                    "interplay_rename_prefix", "interplay_rename_suffix"]
        for key in str_keys:
            if key in self._controls and key in p:
                self._controls[key].setStringValue_(str(p[key]))
        int_keys = ["extend_count", "interplay_workspace_steps",
                    "interplay_rename_trim_start", "interplay_rename_trim_end", "http_port"]
        for key in int_keys:
            if key in self._controls and key in p:
                self._controls[key].setStringValue_(str(p[key]))
        float_keys = ["target_lufs", "max_truepeak"]
        for key in float_keys:
            if key in self._controls and key in p:
                self._controls[key].setStringValue_(str(p[key]))
        if "http_bind_host" in self._controls and "http_bind_host" in p:
            target_ip = p["http_bind_host"]
            for i, (_, ip) in enumerate(self._iface_list):
                if ip == target_ip:
                    self._controls["http_bind_host"].selectItemAtIndex_(i)
                    break

    def onLoadPreset_(self, sender):
        idx = self._preset_popup.indexOfSelectedItem()
        presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if 0 <= idx < len(presets):
            p = presets[idx]
            self._apply_config(
                p.get("rec_a", []),
                p.get("rec_b", []),
                p.get("export_tracks", p.get("export", [])),
                p.get("loudness_tracks", []),
                p.get("play_monitor_tracks", []))
            # mon_a/mon_b are no longer in the UI — store directly in settings
            self._app.settings["monitor_tracks"] = p.get("mon_a", [])
            self._app.settings["monitor_tracks_b"] = p.get("mon_b", [])
            self._apply_all_controls(p)
            logging.info(f"Preset {p.get('name')} geladen.")

    def onSavePreset_(self, sender):
        idx = self._preset_popup.indexOfSelectedItem()
        presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if 0 <= idx < len(presets):
            rec_a, rec_b, export_list, loud_list, play_list = self._read_current_config()
            p = presets[idx]
            p["rec_a"] = rec_a
            p["mon_a"] = self._app.settings.get("monitor_tracks", [])
            p["rec_b"] = rec_b
            p["mon_b"] = self._app.settings.get("monitor_tracks_b", [])
            p["export_tracks"] = export_list
            p["loudness_tracks"] = loud_list
            p["play_monitor_tracks"] = play_list
            p.update(self._read_all_controls())
            self._app.settings["track_presets"] = presets
            save_settings(self._app.settings)
            name = p.get("name", f"Preset {idx+1}")
            logging.info(f"Preset {name} gespeichert.")
            rumps.alert(t("alert_preset_saved"), f"{name} " + t("msg_preset_saved_body"))

    def onRenamePreset_(self, sender):
        idx = self._preset_popup.indexOfSelectedItem()
        presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if 0 <= idx < len(presets):
            old_name = presets[idx].get("name", f"Preset {idx+1}")
            response = rumps.Window(
                title=t("title_preset_rename"), message=t("msg_preset_rename_body").format(old_name),
                default_text=old_name, ok=t("btn_rename"), cancel=t("cancel")).run()
            if response.clicked:
                new_name = response.text.strip()
                if new_name:
                    presets[idx]["name"] = new_name
                    self._app.settings["track_presets"] = presets
                    save_settings(self._app.settings)
                    self._preset_popup.itemAtIndex_(idx).setTitle_(new_name)
                    logging.info(f"Preset umbenannt: {old_name} -> {new_name}")

    def onNewPreset_(self, sender):
        response = rumps.Window(
            title=t("title_preset_new"), message=t("msg_preset_new_body"),
            default_text=t("default_preset_name"), ok=t("btn_new"), cancel=t("cancel")).run()
        if response.clicked:
            new_name = response.text.strip() or t("default_preset_name")
            presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
            new_preset = {"name": new_name, "rec_a": [], "mon_a": [], "rec_b": [], "mon_b": [],
                          "export_tracks": [], "loudness_tracks": [], "play_monitor_tracks": []}
            presets.append(new_preset)
            self._app.settings["track_presets"] = presets
            save_settings(self._app.settings)
            self._preset_popup.addItemWithTitle_(new_name)
            self._preset_popup.selectItemAtIndex_(len(presets) - 1)
            logging.info(f"Preset {new_name} erstellt.")

    def onDeletePreset_(self, sender):
        idx = self._preset_popup.indexOfSelectedItem()
        presets = self._app.settings.get("track_presets", DEFAULT_SETTINGS["track_presets"])
        if len(presets) <= 1:
            rumps.alert(t("alert_preset_delete_error"), t("msg_preset_delete_error_body"))
            return
        if 0 <= idx < len(presets):
            name = presets[idx].get("name", f"Preset {idx+1}")
            presets.pop(idx)
            self._app.settings["track_presets"] = presets
            save_settings(self._app.settings)
            self._preset_popup.removeItemAtIndex_(idx)
            new_idx = min(idx, len(presets) - 1)
            self._preset_popup.selectItemAtIndex_(new_idx)
            logging.info(f"Preset {name} gelöscht.")

    def onSave_(self, sender):
        try:
            # 1. Spuren speichern (ohne monitor_tracks — nur via Presets steuerbar)
            rec_a, rec_b, export_list, loud_list, play_list = self._read_current_config()
            self._app.settings["tracks"] = rec_a
            self._app.settings["tracks_b"] = rec_b
            self._app.settings["export_tracks"] = export_list
            self._app.settings["loudness_tracks"] = loud_list
            self._app.settings["play_monitor_tracks"] = play_list

            # 2. Erweiterte Einstellungen speichern
            l_enabled = (self._controls["loudness_enabled"].state() == AppKit.NSOnState)
            t_lufs = float(self._controls["target_lufs"].stringValue().replace(",", "."))
            m_tp = float(self._controls["max_truepeak"].stringValue().replace(",", "."))
            
            if "interplay_enabled" in self._controls:
                self._app.settings["interplay_enabled"] = (self._controls["interplay_enabled"].state() == AppKit.NSOnState)
            if "interplay_workspace" in self._controls:
                self._app.settings["interplay_workspace"] = self._controls["interplay_workspace"].stringValue()
                
            i_steps = int(self._controls["interplay_workspace_steps"].stringValue())
            e_tc = self._controls["export_start_tc"].stringValue().strip()

            tc_parts = e_tc.split(":")
            if len(tc_parts) != 4 or not all(p.isdigit() for p in tc_parts):
                raise ValueError(t("msg_invalid_tc").format(e_tc))
            if i_steps < 0:
                raise ValueError(t("msg_steps_negative"))

            if "wav_export_enabled" in self._controls:
                self._app.settings["wav_export_enabled"] = (self._controls["wav_export_enabled"].state() == AppKit.NSOnState)
            if "aaf_export_enabled" in self._controls:
                self._app.settings["aaf_export_enabled"] = (self._controls["aaf_export_enabled"].state() == AppKit.NSOnState)

            v_track = self._controls["video_track"].stringValue().strip()
            if not v_track:
                raise ValueError(t("msg_video_track_empty"))

            self._app.settings["video_track"] = v_track
            self._app.settings["export_start_tc"] = e_tc
            self._app.settings["loudness_enabled"] = l_enabled
            self._app.settings["target_lufs"] = t_lufs
            self._app.settings["max_truepeak"] = m_tp
            self._app.settings["interplay_workspace_steps"] = i_steps

            if "export_error_keywords" in self._controls:
                self._app.settings["export_error_keywords"] = (
                    self._controls["export_error_keywords"].stringValue().strip())
            if "export_success_keywords" in self._controls:
                self._app.settings["export_success_keywords"] = (
                    self._controls["export_success_keywords"].stringValue().strip())

            # Custom Export-Pfade
            for _pkey in ("wav_export_path", "aaf_embedded_export_path",
                          "aaf_reference_export_path"):
                if _pkey in self._controls:
                    self._app.settings[_pkey] = self._controls[_pkey].stringValue().strip()

            if "extend_count" in self._controls:
                ext_count = int(self._controls["extend_count"].stringValue())
                if ext_count < 0:
                    raise ValueError(t("msg_extend_count_negative"))
                self._app.settings["extend_count"] = ext_count

            # 3. Monitoring / Play Custom speichern
            for ch_idx in range(1, 3):
                track_key    = f"play_custom_ch{ch_idx}_track"
                mute_s_key   = f"play_custom_ch{ch_idx}_mute_start"
                mute_end_key = f"play_custom_ch{ch_idx}_mute_stop"
                if track_key in self._controls:
                    self._app.settings[track_key] = self._controls[track_key].titleOfSelectedItem() or ""
                if mute_s_key in self._controls:
                    self._app.settings[mute_s_key] = (self._controls[mute_s_key].state() == AppKit.NSOnState)
                if mute_end_key in self._controls:
                    self._app.settings[mute_end_key] = (self._controls[mute_end_key].state() == AppKit.NSOnState)

            # 3b. Audio verschieben (Spur-Move) speichern – nur vollständige
            # Paare (Quelle UND Ziel gesetzt). Dadurch ist die Anzahl der
            # Quell- und Ziel-Spuren immer gleich.
            move_pairs = getattr(self._app, "_move_pairs", None)
            if move_pairs is not None:
                self._app.settings["move_audio_source_tracks"] = [
                    p[0] for p in move_pairs if p[0] and p[1]]
                self._app.settings["move_audio_target_tracks"] = [
                    p[1] for p in move_pairs if p[0] and p[1]]

            # 4. Import-Einstellungen speichern
            if "import_close_session" in self._controls:
                imp_close = (self._controls["import_close_session"].state() == AppKit.NSOnState)
                self._app.settings["import_close_session"] = imp_close

            # 4b. Vocaster-Einstellungen speichern
            if "vocaster_phantom_on_start" in self._controls:
                self._app.settings["vocaster_phantom_on_start"] = (
                    self._controls["vocaster_phantom_on_start"].state() == AppKit.NSOnState)
            if "vocaster_apply_routing_on_start" in self._controls:
                self._app.settings["vocaster_apply_routing_on_start"] = (
                    self._controls["vocaster_apply_routing_on_start"].state() == AppKit.NSOnState)

            # 4. Rename Sequence Einstellungen
            if "interplay_rename_enabled" in self._controls:
                self._app.settings["interplay_rename_enabled"] = (
                    self._controls["interplay_rename_enabled"].state() == AppKit.NSOnState)
                try:
                    self._app.settings["interplay_rename_trim_start"] = max(0, int(
                        self._controls["interplay_rename_trim_start"].stringValue()))
                    self._app.settings["interplay_rename_trim_end"] = max(0, int(
                        self._controls["interplay_rename_trim_end"].stringValue()))
                except ValueError:
                    pass
                self._app.settings["interplay_rename_prefix"] = (
                    self._controls["interplay_rename_prefix"].stringValue())
                self._app.settings["interplay_rename_suffix"] = (
                    self._controls["interplay_rename_suffix"].stringValue())

            # 5. Webtrigger-Port und Interface speichern
            need_http_restart = False
            if "http_port" in self._controls:
                try:
                    new_port = int(self._controls["http_port"].stringValue())
                    if not (1024 <= new_port <= 65535):
                        raise ValueError(t("msg_invalid_port"))
                    old_port = self._app.settings.get("http_port", 8899)
                    self._app.settings["http_port"] = new_port
                    if new_port != old_port:
                        need_http_restart = True
                except ValueError as ve:
                    if str(ve) == t("msg_invalid_port"):
                        raise ve
                    raise ValueError(t("msg_invalid_port_value").format(ve))

            if "http_bind_host" in self._controls:
                idx_iface = self._controls["http_bind_host"].indexOfSelectedItem()
                if 0 <= idx_iface < len(self._iface_list):
                    new_host = self._iface_list[idx_iface][1]
                    old_host = self._app.settings.get("http_bind_host", "127.0.0.1")
                    self._app.settings["http_bind_host"] = new_host
                    if new_host != old_host:
                        need_http_restart = True

            if "webtrigger_token" in self._controls:
                new_token = self._controls["webtrigger_token"].stringValue().strip()
                old_token = self._app.settings.get("webtrigger_token", "")
                self._app.settings["webtrigger_token"] = new_token
                if new_token != old_token:
                    need_http_restart = True

            # 6. Sprache speichern und Menü-Titel sofort aktualisieren
            if "language" in self._controls:
                langs_codes = ["de", "en", "fr", "es", "pt"]
                idx_lang = self._controls["language"].indexOfSelectedItem()
                if 0 <= idx_lang < len(langs_codes):
                    new_lang = langs_codes[idx_lang]
                    self._app.settings["language"] = new_lang
                    set_language(new_lang)
                    self._app.update_menu_titles()

            save_settings(self._app.settings)
            self._window.close()
            logging.info("Alle Einstellungen gespeichert.")

            if need_http_restart:
                import threading as _thr
                _thr.Thread(target=self._app._restart_http, daemon=True).start()
                rumps.alert(t("alert_saved"), t("msg_settings_saved_http_restart"))
            else:
                rumps.alert(t("alert_saved"), t("msg_settings_saved_success"))
        except ValueError as e:
            rumps.alert(t("alert_error"), str(e))

    def onCancel_(self, sender):
        self._window.close()

    def onPlay_(self, sender):
        self._app._trigger_play()


# ─────────────────────────────────────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────────────────────────────────────
_watchdog_proc = None

def _cleanup_watchdog():
    """Beendet den Watchdog-Prozess beim Schließen von PunchBuddy."""
    global _watchdog_proc
    import sys
    import subprocess as _sp
    
    if getattr(sys, 'frozen', False):
        logging.info("Beende Watchdog App via pkill...")
        try:
            _sp.run(["pkill", "-x", "PunchBuddy_Watchdog"], capture_output=True)
            _sp.run(["pkill", "-f", "MacOS/PunchBuddy_Watchdog$"], capture_output=True)
            logging.info("Watchdog App beendet.")
        except Exception as e:
            logging.debug(f"pkill Watchdog fehlgeschlagen: {e}")
    else:
        if _watchdog_proc is not None:
            logging.info(f"Watchdog Script beenden (PID={_watchdog_proc.pid})...")
            try:
                _watchdog_proc.terminate()
                _watchdog_proc.wait(timeout=3)
                logging.info("Watchdog beendet.")
            except Exception:
                try:
                    _watchdog_proc.kill()
                except Exception:
                    pass
            _watchdog_proc = None

if __name__ == "__main__":

    # Laufzeit-Initialisierung mit Seiteneffekten (Sprache, Settings-Migration).
    # Bewusst hier statt auf Modulebene → Import bleibt nebenwirkungsfrei.
    init_runtime()

    import sys
    import subprocess as _sp
    if getattr(sys, 'frozen', False):
        # Vorher laufende Watchdog-Instanz beenden
        logging.info("Beende ggf. laufende Watchdog-Instanz...")
        try:
            _sp.run(["pkill", "-x", "PunchBuddy_Watchdog"], capture_output=True)
            _sp.run(["pkill", "-f", "MacOS/PunchBuddy_Watchdog$"], capture_output=True)
        except Exception as _ke:
            logging.debug(f"pkill Watchdog (pre-start): {_ke}")
        import time as _t; _t.sleep(0.5)  # kurz warten damit der Prozess weg ist

        # Wir laufen als kompilierte PunchBuddy.app
        my_app_dir = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
        parent_dir = os.path.dirname(my_app_dir)
        target_app = os.path.join(parent_dir, "PunchBuddy_Watchdog.app")

        if os.path.exists(target_app):
            try:
                # -g startet die App im Hintergrund ohne Fokus zu stehlen
                _sp.Popen(["open", "-n", "-g", target_app])
                logging.info(f"Watchdog App gestartet: {target_app}")
            except Exception as e:
                logging.error(f"Konnte Watchdog App nicht starten: {e}")
        else:
            try:
                _sp.Popen(["open", "-n", "-g", "-a", "PunchBuddy_Watchdog"])
                logging.info("Watchdog App via Spotlight gestartet.")
            except Exception as e:
                logging.error(f"Watchdog App Fallback gescheitert: {e}")
    else:
        # Vorher laufende watchdog.py-Instanz beenden (Script-Modus)
        try:
            _sp.run(["pkill", "-f", "watchdog.py"], capture_output=True)
        except Exception:
            pass
        _watchdog_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchdog.py")
        if os.path.exists(_watchdog_path):
            try:
                _watchdog_proc = _sp.Popen(
                    [sys.executable, _watchdog_path],
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
                logging.info(f"Watchdog Script gestartet (💀) PID={_watchdog_proc.pid}")
            except Exception as _we:
                logging.warning(f"Watchdog konnte nicht gestartet werden: {_we}")

    # Signal-Handler: SIGTERM/SIGINT → Watchdog beenden + App stoppen
    import signal

    def _signal_handler(signum, frame):
        logging.info(f"Signal {signum} empfangen – Engine schliessen + Watchdog beenden...")
        try:
            if state.app_ref is not None:
                state.app_ref._close_unix_socket()  # Socket-Datei entfernen
        except Exception as _se:
            logging.warning(f"Signal-Handler: _close_unix_socket fehlgeschlagen: {_se}")
        try:
            _close_engine()  # gRPC/PTSL-Channel sauber schliessen (kein CLOSE_WAIT auf PT-Seite)
        except Exception as _ce:
            logging.warning(f"Signal-Handler: _close_engine fehlgeschlagen: {_ce}")
        _cleanup_watchdog()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    atexit.register(_cleanup_watchdog)
    # Engine bei JEDEM Beendigungspfad genau einmal schliessen (idempotent via _engine_lock/None-Check)
    atexit.register(_close_engine)

    PunchBuddyApp().run()
