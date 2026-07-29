#!/usr/bin/env python3
"""
Watchdog für PunchBuddy – läuft als separater Prozess.
Zeigt ☠ in der Menüleiste und kann das Hauptscript killen/neustarten.
"""
import subprocess
import os
import signal
import sys

# ── Dock-Name sofort setzen (VOR import rumps!) ──────────────────────────
try:
    from Foundation import NSBundle, NSProcessInfo
    _bundle = NSBundle.mainBundle()
    _info = _bundle.infoDictionary()
    if _info is not None:
        _info['CFBundleName'] = 'Watchdog PunchBuddy'
        _info['CFBundleDisplayName'] = 'Watchdog PunchBuddy'
    NSProcessInfo.processInfo().setProcessName_('Watchdog PunchBuddy')
except Exception:
    pass

try:
    import ctypes, ctypes.util
    _libc = ctypes.cdll.LoadLibrary(ctypes.util.find_library('c'))
    _libc.setprogname(b'WatchdogPunchBuddy')
except Exception:
    pass

import rumps

MAIN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_punch_in.py")
PYTHON = sys.executable


def _find_main_pids():
    """Findet alle PIDs des Hauptscripts (außer uns selbst)."""
    my_pid = os.getpid()
    pids = set()
    
    # 1. Suche nach Python-Script
    try:
        out = subprocess.check_output(["pgrep", "-f", "auto_punch_in.py"], text=True).strip()
        for line in out.splitlines():
            pid = int(line.strip())
            if pid != my_pid:
                pids.add(pid)
    except Exception:
        pass

    # 2. Suche nach der kompilierten App
    try:
        out = subprocess.check_output(["pgrep", "-x", "PunchBuddy"], text=True).strip()
        for line in out.splitlines():
            pid = int(line.strip())
            if pid != my_pid:
                pids.add(pid)
    except Exception:
        pass

    try:
        out = subprocess.check_output(["pgrep", "-f", "MacOS/PunchBuddy$"], text=True).strip()
        for line in out.splitlines():
            pid = int(line.strip())
            if pid != my_pid:
                pids.add(pid)
    except Exception:
        pass

    return list(pids)


class WatchdogApp(rumps.App):
    def __init__(self):
        super().__init__("💀", icon=None)
        self.menu = [
            rumps.MenuItem("🔄 Script neustarten", callback=self._restart),
            rumps.MenuItem("⛔ Script beenden", callback=self._kill),
            None,
            rumps.MenuItem("📋 Status", callback=self._status),
        ]
        # Tooltip nach App-Start setzen
        self._tooltip_timer = rumps.Timer(self._set_tooltip, 1)
        self._tooltip_timer.start()

    def _set_tooltip(self, timer):
        """Setzt Tooltip, Dock-Name und Dock-Icon nach App-Start."""
        timer.stop()
        try:
            if hasattr(self, '_nsapp') and self._nsapp:
                nsstatusitem = getattr(self._nsapp, 'nsstatusitem', None)
                if nsstatusitem:
                    nsstatusitem.button().setToolTip_("Watchdog PunchBuddy")
        except Exception:
            pass

        # Dock-Name und Icon setzen (statt "Python" → "Watchdog PunchBuddy")
        try:
            import AppKit

            ns_app = AppKit.NSApplication.sharedApplication()

            # CFBundleName überschreiben → Dock zeigt "Watchdog PunchBuddy"
            bundle = AppKit.NSBundle.mainBundle()
            info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
            if info:
                info['CFBundleName'] = 'Watchdog PunchBuddy'

            # Activation Policy auf Regular → App erscheint im Dock
            ns_app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)

            # Icon setzen
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "PunchBuddy.png")
            if not os.path.exists(icon_path):
                icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Resources", "PunchBuddy.png")
            if os.path.exists(icon_path):
                icon = AppKit.NSImage.alloc().initWithContentsOfFile_(icon_path)
                if icon:
                    icon.setSize_(AppKit.NSMakeSize(128, 128))
                    ns_app.setApplicationIconImage_(icon)
        except Exception:
            pass

    def _kill(self, _):
        """Beendet das Hauptscript."""
        pids = _find_main_pids()
        if not pids:
            rumps.notification("PunchBuddy Watchdog", "", "Kein laufendes Script gefunden.")
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        rumps.notification("PunchBuddy Watchdog", "", f"Script beendet (PIDs: {pids})")

    def _restart(self, _):
        """Beendet das Hauptscript und startet es neu."""
        pids = _find_main_pids()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

        # Kurz warten, dann neustarten
        import time
        time.sleep(1)
        
        import sys
        if getattr(sys, 'frozen', False):
            # Wir sind als .app kompiliert
            watchdog_app = os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
            parent_dir = os.path.dirname(watchdog_app)
            target_app = os.path.join(parent_dir, "PunchBuddy.app")
            
            if os.path.exists(target_app):
                subprocess.Popen(["open", "-n", target_app])
            else:
                subprocess.Popen(["open", "-n", "-a", "PunchBuddy"])
        else:
            # Wir laufen als Python-Script
            subprocess.Popen(
                [PYTHON, MAIN_SCRIPT],
                cwd=os.path.dirname(MAIN_SCRIPT),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        rumps.notification("PunchBuddy Watchdog", "", "Script wurde neugestartet.")

    def _status(self, _):
        """Zeigt den Status des Hauptscripts."""
        pids = _find_main_pids()
        if pids:
            rumps.notification("PunchBuddy Watchdog", "", f"Script läuft (PIDs: {pids})")
        else:
            rumps.notification("PunchBuddy Watchdog", "", "Script läuft NICHT!")


if __name__ == "__main__":
    WatchdogApp().run()
