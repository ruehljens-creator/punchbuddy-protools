#!/usr/bin/env python3
"""Pro Tools MCP-Server für PunchBuddy.

Stellt lesende Pro-Tools-Abfragen (über PTSL / py-ptsl) als MCP-Tools bereit.
Wiederverwendet die robuste Engine-Schicht aus punchbuddy.engine
(Singleton-Verbindung, gRPC-Deadline, serialisierte Aufrufe).

Bewusst nur lesend – keine Transport-/Aufnahme-Aktionen, damit eine laufende
Session nicht versehentlich verändert wird. Schreibende Tools können bei Bedarf
ergänzt werden.
"""
import os
import sys

# Repo-Wurzel auf den Importpfad, damit `punchbuddy` gefunden wird.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from mcp.server.fastmcp import FastMCP  # noqa: E402
from punchbuddy.engine import _get_engine, _ptsl_call, _get_preroll_status  # noqa: E402

mcp = FastMCP("protools")


def _call(method_name: str, timeout: float = 8.0):
    """Holt die Engine und ruft eine parameterlose Engine-Methode über
    _ptsl_call auf. Gibt (ok, result-or-error) zurück."""
    eng = _get_engine()
    if eng is None:
        return False, "PTSL nicht verfügbar – läuft Pro Tools und ist PTSL aktiv?"
    fn = getattr(eng, method_name, None)
    if fn is None:
        return False, f"Engine hat keine Methode '{method_name}'"
    ok, res = _ptsl_call(fn, label=f"mcp:{method_name}", timeout=timeout)
    return ok, res


@mcp.tool()
def pt_transport_state() -> str:
    """Aktueller Transport-Zustand von Pro Tools (z. B. Stopped, Playing,
    Recording)."""
    ok, res = _call("transport_state")
    return str(res) if ok else f"Fehler: {res}"


@mcp.tool()
def pt_track_list() -> str:
    """Liste aller Spuren der aktuellen Session mit Name und Typ."""
    ok, res = _call("track_list")
    if not ok:
        return f"Fehler: {res}"
    if not res:
        return "Keine Spuren gefunden."
    lines = []
    for t in res:
        name = getattr(t, "name", "?")
        ttype = getattr(t, "type", "?")
        lines.append(f"- {name} (type={ttype})")
    return f"{len(lines)} Spuren:\n" + "\n".join(lines)


@mcp.tool()
def pt_session_info() -> str:
    """Basis-Infos zur aktuellen Session: Name, Pfad, Samplerate, Timecode-Rate."""
    out = {}
    for label, method in (("name", "session_name"), ("path", "session_path"),
                          ("sample_rate", "session_sample_rate"),
                          ("timecode_rate", "session_timecode_rate")):
        ok, res = _call(method)
        out[label] = str(res) if ok else f"<Fehler: {res}>"
    return "\n".join(f"{k}: {v}" for k, v in out.items())


@mcp.tool()
def pt_timeline_selection() -> str:
    """Aktuelle Timeline-Auswahl inkl. Pre-Roll-Status."""
    eng = _get_engine()
    if eng is None:
        return "PTSL nicht verfügbar."
    ok, preroll = _get_preroll_status(eng)
    return f"preroll_enabled={preroll}" if ok else "Konnte Auswahl nicht lesen."


@mcp.tool()
def pt_memory_locations() -> str:
    """Memory Locations (Marker) der aktuellen Session."""
    ok, res = _call("get_memory_locations")
    if not ok:
        return f"Fehler: {res}"
    if not res:
        return "Keine Memory Locations."
    lines = []
    for m in res:
        num = getattr(m, "number", "?")
        name = getattr(m, "name", "")
        lines.append(f"- #{num} {name}")
    return f"{len(lines)} Memory Locations:\n" + "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
