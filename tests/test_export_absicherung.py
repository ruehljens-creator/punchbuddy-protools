"""2.0.3 – Absicherung des Exports (Studio-Befund 14.09.2026: Interplay-Export
lief ohne Consolidate bis zum Ende und meldete Erfolg).

1. Video-Ende wird mehrfach versucht; ohne Ergebnis (None, None).
2. Consolidate gilt nur als gelungen, wenn NEUE Audiodateien entstanden sind
   (juenger als der Exportstart); alte Dateien frueherer Exporte zaehlen nicht.
3. Ohne Nachweis: ein zweiter Versuch mit erneuerter Auswahl, dann Abbruch.
Kein Test erreicht Pro Tools (Fake-Engine, Fensterabfrage ersetzt)."""
import os
import time

import pytest

import punchbuddy.export as E


class _Engine:
    def __init__(self, auswahlen, dateien_beim_consolidate=None, audio_dir=None):
        self.auswahlen = list(auswahlen)        # Antworten von get_timeline_selection, der Reihe nach
        self.calls = []
        self.dateien = dateien_beim_consolidate or []
        self.audio_dir = audio_dir
        self.consolidates = 0

    def select_all_clips_on_track(self, track):
        self.calls.append(("clips", track))

    def get_timeline_selection(self):
        self.calls.append(("sel",))
        a = self.auswahlen.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    def select_tracks_by_name(self, names): self.calls.append(("tracks", tuple(names)))
    def set_timeline_selection(self, in_time, out_time): self.calls.append(("timeline", in_time, out_time))
    def extend_selection_to_target_tracks(self, names): self.calls.append(("extend", tuple(names)))

    def consolidate_clip(self):
        self.consolidates += 1
        self.calls.append(("consolidate",))
        if self.consolidates <= len(self.dateien):
            for name in self.dateien[self.consolidates - 1]:
                with open(os.path.join(self.audio_dir, name), "wb") as f:
                    f.write(b"\x00" * 100)


@pytest.fixture(autouse=True)
def _ohne_pro_tools(monkeypatch):
    monkeypatch.setattr(E.time, "sleep", lambda s: None)
    monkeypatch.setattr(E, "_wait_for_consolidate_window_gone", lambda timeout=60: True)
    gezeigt = []
    monkeypatch.setattr(E, "_show_error", lambda titel, msg: gezeigt.append((titel, msg)))
    E._gezeigt = gezeigt
    yield


def test_video_ende_kommt_im_zweiten_versuch():
    eng = _Engine([("10:00:00:00.00", "00:00:00:00.00"), ("10:00:00:00.00", "10:05:12:03.00")])
    assert E._video_ende_ermitteln(eng, "Video 1") == ("10:00:00:00.00", "10:05:12:03.00")
    assert eng.calls.count(("sel",)) == 2


def test_video_ende_nach_drei_versuchen_aufgegeben():
    eng = _Engine([RuntimeError("PT busy"), ("x", None), ("x", "00:00:00:00.00")])
    assert E._video_ende_ermitteln(eng, "Video 1", versuche=3) == (None, None)
    assert eng.calls.count(("clips", "Video 1")) == 3


def _session(tmp_path, alt=()):
    audio = tmp_path / "Audio Files"; audio.mkdir()
    for name in alt:
        p = audio / name; p.write_bytes(b"\x00" * 50)
        os.utime(p, (time.time() - 3600, time.time() - 3600))      # eine Stunde alt
    return str(tmp_path), str(audio)


def test_alte_dateien_zaehlen_nicht_als_consolidate(tmp_path):
    sdir, audio = _session(tmp_path, alt=["ST_01.wav", "Spr_01.wav"])
    assert E._wait_for_consolidated_files(sdir, ["ST", "Spr"], timeout=0.3, min_mtime=time.time() - 5) is False
    assert E._wait_for_consolidated_files(sdir, ["ST", "Spr"], timeout=0.3) is True      # ohne Filter wie frueher


def test_consolidate_mit_nachweis_erfolg_beim_ersten_mal(tmp_path):
    sdir, audio = _session(tmp_path, alt=["ST_01.wav"])
    eng = _Engine([], dateien_beim_consolidate=[["ST_02.wav", "Spr_01.wav"]], audio_dir=audio)
    assert E._consolidate_mit_nachweis(eng, sdir, ["ST", "Spr"], "10:00:00:00.00", "10:01:00:00.00", time.time()) is True
    assert eng.consolidates == 1


def test_consolidate_wiederholt_einmal_mit_neuer_auswahl(tmp_path):
    sdir, audio = _session(tmp_path, alt=["ST_01.wav"])
    eng = _Engine([], dateien_beim_consolidate=[[], ["ST_02.wav"]], audio_dir=audio)
    assert E._consolidate_mit_nachweis(eng, sdir, ["ST"], "10:00:00:00.00", "10:01:00:00.00", time.time()) is True
    assert eng.consolidates == 2
    assert ("extend", ("ST",)) in eng.calls                       # Auswahl vor dem zweiten Versuch erneuert


def test_consolidate_ohne_neue_dateien_bricht_ab(tmp_path):
    sdir, audio = _session(tmp_path, alt=["ST_01.wav"])
    eng = _Engine([], dateien_beim_consolidate=[[], []], audio_dir=audio)
    with pytest.raises(E.ExportAbbruch):
        E._consolidate_mit_nachweis(eng, sdir, ["ST"], "10:00:00:00.00", "10:01:00:00.00", time.time())
    assert eng.consolidates == 2


def test_interplay_export_bricht_ohne_video_ende_ab(monkeypatch, tmp_path):
    """Der ganze Interplay-Export: ohne Video-Ende kein F13, kein 'abgeschlossen'."""
    eng = _Engine([("x", None)] * 3)
    eng.session_path = lambda: str(tmp_path / "S.ptx")
    eng.set_track_hidden_state = lambda names, state: None
    monkeypatch.setattr(E, "_get_engine", lambda: eng)
    monkeypatch.setattr(E, "_ensure_transport_stopped", lambda e: None)
    monkeypatch.setattr(E, "_detect_video_track", lambda e, s=None: "Video 1")
    monkeypatch.setattr(E, "_show_progress_win", lambda titel: {"update": lambda *a: None, "close": lambda: None})
    monkeypatch.setattr(E, "_set_busy", lambda b: None)
    gesendet = []
    monkeypatch.setattr(E, "_send_key", lambda *a, **k: gesendet.append(a))
    ok = E.run_interplay_export(["ST", "Spr"], {"export_start_tc": "10:00:00:00"})
    assert ok is False
    assert gesendet == []                                           # kein F13, kein Return
    assert E._gezeigt and "Video-Ende" in E._gezeigt[0][1]
    assert eng.consolidates == 0
