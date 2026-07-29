"""Zentrale Versionsnummer von PunchBuddy (einzige Quelle der Wahrheit).

Semantische Versionierung (MAJOR.MINOR.PATCH):
  MAJOR – Architektur-/Bruchänderungen
  MINOR – neue Funktionen
  PATCH – Fehlerbehebungen

Wird verwendet von: Startup-Log, Menüleisten-Tooltip, Diagnose-Report
(Sektion 1), Build-Skripten (CFBundleShortVersionString + DMG-Dateiname).
Beim Release zusätzlich per Git-Tag markieren:  git tag vX.Y.Z && git push origin vX.Y.Z
"""

__version__ = "2.0.2"
