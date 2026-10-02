"""Tests für normalize_track (ohne Pro Tools)."""
import os

import numpy as np
import pyloudnorm as pyln
import pytest
import soundfile as sf

import punchbuddy.loudness as loudness

RATE = 48000


class _FakeEngine:
    """Ersetzt Pro Tools; Umbenennen schlägt fehl, damit der Dateiname bleibt."""
    def refresh_all_modified_audio_files(self):
        pass

    def rename_target_clip(self, *a, **k):
        raise RuntimeError("Attrappe")


@pytest.fixture(autouse=True)
def _ohne_wartezeit(monkeypatch):
    monkeypatch.setattr(loudness.time, "sleep", lambda s: None)


def _session(tmp_path):
    os.makedirs(tmp_path / "Audio Files")
    return str(tmp_path)


def _rauschen(kanaele=2, sekunden=5, pegel=0.02, seed=1):
    rng = np.random.default_rng(seed)
    form = (sekunden * RATE, kanaele) if kanaele > 1 else (sekunden * RATE,)
    return rng.standard_normal(form) * pegel


def _lufs(x):
    return pyln.Meter(RATE).integrated_loudness(x)


def test_schreibt_24_bit(tmp_path):
    d = _session(tmp_path)
    f = os.path.join(d, "Audio Files", "ST_02.wav")
    sf.write(f, _rauschen(), RATE, subtype="PCM_24")

    loudness.normalize_track(_FakeEngine(), d, "ST", -23.0, -3.0)

    info = sf.info(f)
    assert info.subtype == "PCM_24"
    assert info.samplerate == RATE
    data, _ = sf.read(f)
    assert _lufs(data) == pytest.approx(-23.0, abs=0.1)
