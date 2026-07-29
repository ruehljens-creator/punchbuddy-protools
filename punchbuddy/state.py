"""Geteilter, veränderlicher Laufzeit-Zustand (Transport-/Aufnahme-Flags).

Wird modulübergreifend gelesen/geschrieben: Transport/Export setzen die Flags,
Keep-Alive und Webtrigger lesen sie. Zugriff als Attribut, z. B.
`state.running = True`.
"""
import threading

running = False
running_lock = threading.Lock()
import_running = False
export_running = False
play_monitor_tracks = []
play_custom_active = False

# ── App-Singleton + Menüleisten-Titel-Icons ──────────────────────────
app_ref = None          # wird in PunchBuddyApp.__init__ gesetzt
ICON_IDLE = "⏺"        # normaler Zustand
ICON_BUSY = "🔴"        # Script arbeitet
