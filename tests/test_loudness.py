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


def _mit_zwischenspitzen(ziel_sample_peak_db=-3.5):
    """Rauschen plus 1-ms-Bursts bei fs/4 mit 45° Phase.

    Nach der Normalisierung auf -23 LUFS liegt der Sample-Peak bei
    `ziel_sample_peak_db`, der True Peak gut 3 dB höher.
    """
    basis = _rauschen(sekunden=10)
    t = np.arange(48)
    burst = np.sin(2 * np.pi * (RATE / 4) * t / RATE + np.pi / 4)

    def bauen(a):
        x = basis.copy()
        for start in range(RATE, len(x) - 48, 2 * RATE):
            x[start:start + 48, :] = a * burst[:, None]
        return x

    a = 0.1
    for _ in range(6):
        gain = 10 ** ((-23 - _lufs(bauen(a))) / 20)
        a = 10 ** (ziel_sample_peak_db / 20) / (0.7071 * gain)
    return bauen(a)


def _tp_voll(x):
    from scipy.signal import resample_poly
    return 20 * np.log10(np.max(np.abs(resample_poly(x, 4, 1, axis=0))))


def test_true_peak_blockweise_wie_am_stueck():
    x = _mit_zwischenspitzen()
    assert loudness._true_peak_db(x, block=5000) == pytest.approx(_tp_voll(x), abs=1e-6)


def test_true_peak_wird_eingehalten(tmp_path):
    d = _session(tmp_path)
    f = os.path.join(d, "Audio Files", "ST_02.wav")
    sf.write(f, _mit_zwischenspitzen(-3.5), RATE, subtype="PCM_24")

    loudness.normalize_track(_FakeEngine(), d, "ST", -23.0, -3.0)

    data, _ = sf.read(f)
    # Der Sample-Peak allein (-3,5 dBFS) hätte keine Begrenzung ausgelöst.
    assert _tp_voll(data) <= -3.0 + 0.01


def test_split_mono_paar_gemeinsam(tmp_path):
    d = _session(tmp_path)
    links = _rauschen(kanaele=1, seed=2)
    rechts = _rauschen(kanaele=1, seed=3)
    fl = os.path.join(d, "Audio Files", "ST_03.L.wav")
    fr = os.path.join(d, "Audio Files", "ST_03.R.wav")
    sf.write(fl, links, RATE, subtype="PCM_24")
    sf.write(fr, rechts, RATE, subtype="PCM_24")

    loudness.normalize_track(_FakeEngine(), d, "ST", -23.0, -3.0)

    nl, _ = sf.read(fl)
    nr, _ = sf.read(fr)
    assert sf.info(fl).subtype == sf.info(fr).subtype == "PCM_24"
    # beide Kanäle mit demselben Gain, als Stereopaar auf -23 LUFS
    assert np.std(nl) / np.std(links) == pytest.approx(np.std(nr) / np.std(rechts), rel=1e-3)
    assert _lufs(np.column_stack([nl, nr])) == pytest.approx(-23.0, abs=0.1)
