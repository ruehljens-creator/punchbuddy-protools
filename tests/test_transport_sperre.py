"""Strenge Transport-Sperre des Exports (Vorgabe Jens, 02.10.2026).

Laeuft beim Export noch Wiedergabe oder Aufnahme, kann die Lautheitskorrektur Pro Tools
zum Absturz bringen. Deshalb: Stop senden und auf Bestaetigung warten; ist der Stillstand
nicht bestaetigt oder der Zustand nicht lesbar, bricht der Export ab (frueher: "trotzdem
fortfahren"). Vor jeder Lautheitskorrektur wird erneut geprueft.
Kein Test erreicht Pro Tools (Fake-Engine)."""
import pytest

import punchbuddy.export as E
import punchbuddy.loudness as L


class _Engine:
    def __init__(self, zustaende):
        self.zustaende = list(zustaende)       # Antworten von transport_state, der Reihe nach
        self.calls = []

    def transport_state(self):
        self.calls.append("state")
        z = self.zustaende.pop(0) if len(self.zustaende) > 1 else self.zustaende[0]
        if isinstance(z, Exception):
            raise z
        return z

    def toggle_play_state(self):
        self.calls.append("toggle")


@pytest.fixture(autouse=True)
def _ohne_pro_tools(monkeypatch):
    monkeypatch.setattr(E.time, "sleep", lambda s: None)
    monkeypatch.setattr(L.time, "sleep", lambda s: None)
    yield


def test_steht_schon():
    eng = _Engine(["TS_TransportStopped"])
    E._transport_muss_stehen(eng)
    assert "toggle" not in eng.calls


def test_wiedergabe_wird_angehalten():
    eng = _Engine(["TS_TransportPlaying", "TS_TransportPlaying", "TS_TransportStopped"])
    E._transport_muss_stehen(eng)
    assert eng.calls.count("toggle") == 1


def test_aufnahme_haelt_nicht_an_abbruch():
    eng = _Engine(["TS_TransportRecording"])
    with pytest.raises(E.ExportAbbruch, match="Export: Pro Tools steht nicht \\(TS_TransportRecording\\)"):
        E._transport_muss_stehen(eng)
    assert eng.calls.count("toggle") == 1                     # nur einmal umschalten


def test_zustand_nicht_lesbar_abbruch():
    eng = _Engine([RuntimeError("keine Antwort")])
    with pytest.raises(E.ExportAbbruch, match="nicht lesbar"):
        E._transport_muss_stehen(eng)
    assert "toggle" not in eng.calls


def test_lautheit_prueft_vor_jeder_spur(monkeypatch):
    """Die Sperre laeuft vor jeder Spur; bricht sie ab, folgt keine Korrektur mehr."""
    korrigiert = []
    monkeypatch.setattr(L, "normalize_track", lambda engine, d, lt, *a, **k: korrigiert.append(lt))
    monkeypatch.setattr(L, "_dispatch_main", lambda fn: None)  # kein Fenster
    zustaende = iter(["TS_TransportStopped", "TS_TransportRecording"])

    def vor_spur(lt):
        eng = _Engine([next(zustaende)])
        E._transport_muss_stehen(eng, f"Lautheit {lt}")
    with pytest.raises(E.ExportAbbruch, match="^Lautheit Spr: "):
        L._run_loudness_with_progress(object(), "/tmp", ["ST", "Spr"], -23.0, -3.0, vor_spur=vor_spur)
    assert korrigiert == ["ST"]


def test_alle_exportwege_nutzen_die_strenge_sperre():
    import inspect
    quelle = inspect.getsource(E)
    for name in ("run_interplay_export", "run_export", "run_wav_export_standalone",
                 "run_aaf_export_standalone", "run_aaf_reference_export_standalone"):
        code = inspect.getsource(getattr(E, name))
        assert "_transport_muss_stehen(engine)" in code, name
        assert "_ensure_transport_stopped(engine)" not in code, name
    assert quelle.count("vor_spur=lambda lt: _transport_muss_stehen") == 4
