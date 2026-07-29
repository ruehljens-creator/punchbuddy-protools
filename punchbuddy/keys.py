"""Tastendruck-Injektion (CGEvent) und Prozess-PID-Ermittlung.

Eigenständige NSAppleScript-Erkennung, damit das Modul nicht vom Hauptfile
abhängt (kein Zirkularimport).
"""
import os
import logging
import subprocess

try:
    from Foundation import NSAppleScript
    APPLESCRIPT_OK = True
except ImportError:
    NSAppleScript = None
    APPLESCRIPT_OK = False

# ── Weitere modulweite Virtual-Key-Codes (zentralisiert) ──────────────────
_VK_OE = 41  # Ö auf QWERTZ-Tastatur (= Semicolon-Position auf US)
_VK_F13   = 105   # F13
_VK_RETURN = 36   # Enter/Return
_VK_TAB    = 48   # Tab
_VK_DOWN   = 125  # Arrow Down


# macOS Virtual Key Codes
_VK_K     = 40   # 'k'
_VK_F12   = 111  # F12
_VK_SPACE = 49   # Space
_VK_R     = 15   # 'r'
_VK_A     = 0    # 'a'
_VK_F2    = 120  # F2
_VK_C     = 8    # 'c'
_VK_ESC   = 53   # Escape
_VK_P     = 35   # 'p'
_VK_V     = 9    # 'v'

_cached_pid = None

def _pt_pid():
    """Pro Tools Prozess-ID ermitteln (mit Caching).

    Der Cache wird vor jeder Rückgabe gegen den laufenden Prozess validiert:
    Startet Pro Tools neu (Crash/Update), ist die gecachte PID tot und würde
    sonst Tastendrücke ins Leere senden (CGEventPostToPid an tote PID).
    """
    global _cached_pid
    if _cached_pid is not None:
        try:
            os.kill(_cached_pid, 0)  # Signal 0: prüft nur Existenz, sendet nichts
            return _cached_pid
        except OSError:
            logging.info(f"Pro Tools PID {_cached_pid} nicht mehr aktiv – Cache geleert.")
            _cached_pid = None

    try:
        out = subprocess.run(
            ["pgrep", "-x", "Pro Tools"],
            capture_output=True, text=True, timeout=2
        ).stdout.strip()
        if out:
            _cached_pid = int(out)
            return _cached_pid
        return None
    except Exception:
        return None

def _send_key(vk: int, cmd: bool = False) -> bool:
    """
    Sendet Tastendruck direkt an Pro Tools via CGEventPostToPid.
    Non-blocking: kehrt sofort zurück, kein Warten auf PT's Main-Thread.
    Fallback auf AppleScript wenn Quartz nicht verfügbar.
    """
    try:
        import Quartz
        pid = _pt_pid()
        if not pid:
            logging.warning("send_key: Pro Tools PID nicht gefunden.")
            return False
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        ev_dn = Quartz.CGEventCreateKeyboardEvent(src, vk, True)
        ev_up = Quartz.CGEventCreateKeyboardEvent(src, vk, False)
        if cmd:
            flags = Quartz.kCGEventFlagMaskCommand
            Quartz.CGEventSetFlags(ev_dn, flags)
            Quartz.CGEventSetFlags(ev_up, flags)
        Quartz.CGEventPostToPid(pid, ev_dn)
        Quartz.CGEventPostToPid(pid, ev_up)
        return True
    except ImportError:
        logging.debug("Quartz nicht verfügbar – AppleScript Fallback")
        if APPLESCRIPT_OK:
            char = {_VK_K: "k", _VK_F12: "", _VK_SPACE: " "}.get(vk, "")
            if char:
                mod = "{command down}" if cmd else ""
                script = (
                    f'tell application "System Events" to tell process "Pro Tools" to '
                    f'keystroke "{char}"' + (f" using {mod}" if mod else "")
                )
                s = NSAppleScript.alloc().initWithSource_(script)
                s.executeAndReturnError_(None)
        return False
    except Exception as e:
        logging.error(f"send_key vk={vk} cmd={cmd}: {e}")
        return False

# Validierter PID-Cache pro App-Name. Spart auf Hot-Paths (Key-Sends an PT/
# Interplay während Export) die NSWorkspace-Iteration + bis zu zwei pgrep-Forks.
# Vor jeder Rückgabe wird die PID mit os.kill(pid, 0) gegen den laufenden Prozess
# geprüft – nach App-Neustart wird der Eintrag automatisch verworfen.
_app_pid_cache: dict = {}


def _app_pid(app_name: str):
    """Ermittelt die PID einer App anhand ihres Prozessnamens (mit validiertem
    Cache). Nutzt NSWorkspace (leerzeichentolerant) als primäre Methode,
    pgrep als Fallback."""
    cached = _app_pid_cache.get(app_name)
    if cached is not None:
        try:
            os.kill(cached, 0)
            return cached
        except OSError:
            _app_pid_cache.pop(app_name, None)

    found = None
    # NSWorkspace: normalisiert Leerzeichen, damit "interplayAccess" == "Interplay Access"
    try:
        from AppKit import NSWorkspace
        ws = NSWorkspace.sharedWorkspace()
        name_norm = app_name.lower().replace(" ", "")
        for app in ws.runningApplications():
            ln = app.localizedName() or ""
            if name_norm in ln.lower().replace(" ", ""):
                found = app.processIdentifier()
                break
    except Exception:
        pass
    # Fallback: pgrep exakt, dann ohne -x
    if found is None:
        try:
            out = subprocess.run(
                ["pgrep", "-xi", app_name],
                capture_output=True, text=True, timeout=2
            ).stdout.strip()
            if out:
                found = int(out.split("\n")[0])
            else:
                out = subprocess.run(
                    ["pgrep", "-i", app_name],
                    capture_output=True, text=True, timeout=2
                ).stdout.strip()
                if out:
                    found = int(out.split("\n")[0])
        except Exception:
            pass

    if found is not None:
        _app_pid_cache[app_name] = found
    return found

def _send_key_to_app(vk: int, app_name: str, cmd: bool = False,
                      shift: bool = False, ctrl: bool = False) -> bool:
    """
    Sendet Tastendruck an eine beliebige App via CGEventPostToPid.
    Umgeht die osascript Accessibility-Einschränkung.
    """
    try:
        import Quartz
        pid = _app_pid(app_name)
        if not pid:
            logging.warning(f"send_key_to_app: '{app_name}' PID nicht gefunden.")
            return False
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        ev_dn = Quartz.CGEventCreateKeyboardEvent(src, vk, True)
        ev_up = Quartz.CGEventCreateKeyboardEvent(src, vk, False)
        flags = 0
        if cmd:
            flags |= Quartz.kCGEventFlagMaskCommand
        if shift:
            flags |= Quartz.kCGEventFlagMaskShift
        if ctrl:
            flags |= Quartz.kCGEventFlagMaskControl
        if flags:
            Quartz.CGEventSetFlags(ev_dn, flags)
            Quartz.CGEventSetFlags(ev_up, flags)
        Quartz.CGEventPostToPid(pid, ev_dn)
        Quartz.CGEventPostToPid(pid, ev_up)
        return True
    except Exception as e:
        logging.error(f"send_key_to_app vk={vk} app={app_name}: {e}")
        return False

def _activate_app(app_name: str) -> bool:
    """Aktiviert eine App (bringt sie in den Vordergrund) via NSWorkspace.
    Normalisiert Leerzeichen im Namen, damit z.B. "interplayAccess" == "Interplay Access".
    """
    try:
        from AppKit import NSWorkspace
        ws = NSWorkspace.sharedWorkspace()
        apps = ws.runningApplications()
        name_norm = app_name.lower().replace(" ", "")
        for app in apps:
            ln = app.localizedName() or ""
            if name_norm in ln.lower().replace(" ", ""):
                app.activateWithOptions_(0x01)  # NSApplicationActivateIgnoringOtherApps
                return True
        # Fallback: via open command
        subprocess.run(["open", "-a", app_name], timeout=5, capture_output=True)
        return True
    except Exception as e:
        logging.warning(f"activate_app '{app_name}': {e}")
        return False
