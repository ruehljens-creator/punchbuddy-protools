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
    """Referenz: BS.1770-Filter auf das ganze Signal, ohne Bloecke und Abkuerzungen."""
    from scipy.signal import upfirdn
    h, _ = loudness._tp_filter()
    return 20 * np.log10(np.max(np.abs(upfirdn(h.astype(np.float64), x, up=4, axis=0))))


def test_true_peak_schnell_wie_referenz():
    x = _mit_zwischenspitzen()
    assert loudness._true_peak_db(x) == pytest.approx(_tp_voll(x), abs=1e-3)
    mono = x[:, 0] * 0.3
    assert loudness._true_peak_db(mono) == pytest.approx(_tp_voll(mono), abs=1e-3)


def test_true_peak_nahe_an_resample_poly():
    from scipy.signal import resample_poly
    x = _mit_zwischenspitzen()
    alt = 20 * np.log10(np.max(np.abs(resample_poly(x, 4, 1, axis=0))))
    assert loudness._true_peak_db(x) == pytest.approx(alt, abs=0.2)


def test_spitzen_je_sample_richtig_zugeordnet():
    from scipy.signal import resample_poly
    rng = np.random.default_rng(7)
    x = rng.standard_normal((200_000, 2)) * 0.05
    x[123_456] = 0.9                                   # eine deutliche Spitze
    sp = loudness._spitzen_je_sample(x)
    assert abs(int(np.argmax(sp)) - 123_456) <= 1
    # unabhaengige Kontrolle: Spitzen je Sample aus resample_poly (nullphasig);
    # die beste Uebereinstimmung muss ohne Verschiebung liegen
    up = np.abs(resample_poly(x, 4, 1, axis=0)).max(axis=1).reshape(-1, 4).max(axis=1)
    ref = np.maximum(up, np.concatenate(([0.0], up[:-1])))
    mitte = slice(1000, -1000)
    fehler = {k: float(np.mean(np.abs(np.roll(sp, k)[mitte] - ref[mitte]))) for k in (-2, -1, 0, 1, 2)}
    assert min(fehler, key=fehler.get) == 0
    assert sp[123_456] == pytest.approx(ref[123_456], rel=0.03)


def test_spitzen_ausgelassene_bloecke_bleiben_unter_grenze():
    x = _mit_zwischenspitzen(-1.0)
    grenze = 0.3
    sp_schnell = loudness._spitzen_je_sample(x, unter=grenze)
    sp_voll = loudness._spitzen_je_sample(x)
    ueber = sp_voll > grenze
    np.testing.assert_array_equal(sp_schnell[ueber], sp_voll[ueber])   # alles Relevante exakt
    assert np.all(sp_schnell[~ueber] <= grenze)


def test_true_peak_wird_eingehalten(tmp_path):
    d = _session(tmp_path)
    f = os.path.join(d, "Audio Files", "ST_02.wav")
    sf.write(f, _mit_zwischenspitzen(-3.5), RATE, subtype="PCM_24")

    loudness.normalize_track(_FakeEngine(), d, "ST", -23.0, -3.0)

    data, _ = sf.read(f)
    # Der Sample-Peak allein (-3,5 dBFS) hätte keine Begrenzung ausgelöst.
    assert _tp_voll(data) <= -3.0 + 0.1


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


# ── True-Peak-Limiter ────────────────────────────────────────────────────────

def _kurve_von_hand(g, lookahead, alpha_block):
    """Langsame Vergleichsumsetzung von _verstaerkungskurve."""
    n, block = len(g), loudness._LIMITER_BLOCK
    m = np.array([min(g[i:i + lookahead]) for i in range(n)])
    r, rb = 1.0, []
    for j in range(0, n, block):
        r = min(m[j:j + block].min(), r + (1.0 - r) * alpha_block)
        rb.append(r)
    r = np.repeat(rb, block)[:n]
    rp = np.concatenate([np.full(lookahead - 1, r[0]), r])
    return np.array([rp[i:i + lookahead].mean() for i in range(n)])


def test_verstaerkungskurve_wie_von_hand_und_nie_ueber_bedarf():
    rng = np.random.default_rng(5)
    g = np.ones(3000)
    stellen = rng.choice(3000, 25, replace=False)
    g[stellen] = rng.uniform(0.3, 0.99, 25)
    s = loudness._verstaerkungskurve(g, 96, 0.05)
    np.testing.assert_allclose(s, _kurve_von_hand(g, 96, 0.05), atol=1e-12)
    assert np.all(s <= g + 1e-12)


def test_limiter_haelt_grenze_und_laesst_rest_unveraendert():
    x = _mit_zwischenspitzen(-1.0) * 10 ** (8 / 20)   # deutlich ueber -3 dBTP
    y, gr_db = loudness._true_peak_limiter(x, RATE, -3.0)
    assert _tp_voll(y) <= -3.0 + 0.01   # erlaubt 0,1 dB; tatsaechlich Tausendstel
    assert gr_db > 0
    # vor der ersten Spitze (1 s) bleibt alles bitgleich
    np.testing.assert_array_equal(y[:RATE - 2000], x[:RATE - 2000])


def test_limiter_mono():
    x = _mit_zwischenspitzen(-1.0)[:, 0] * 10 ** (8 / 20)
    y, _ = loudness._true_peak_limiter(x, RATE, -3.0)
    assert y.ndim == 1
    assert _tp_voll(y) <= -3.0 + 0.01


def test_lautheit_bleibt_beim_ziel_mit_limiter(tmp_path):
    d = _session(tmp_path)
    f = os.path.join(d, "Audio Files", "ST_02.wav")
    sf.write(f, _mit_zwischenspitzen(-3.5), RATE, subtype="PCM_24")

    loudness.normalize_track(_FakeEngine(), d, "ST", -23.0, -3.0)

    data, _ = sf.read(f)
    assert _tp_voll(data) <= -3.0 + 0.01
    assert _lufs(data) == pytest.approx(-23.0, abs=0.1)
    meta = open(os.path.join(d, "Loudness Correction Metadata.txt"), encoding="utf-8").read()
    assert "Norm konform:         JA" in meta


def test_verstaerkungskurve_spitze_am_dateianfang():
    g = np.ones(500)
    g[3] = 0.5
    s = loudness._verstaerkungskurve(g, 96, 0.05)
    assert np.all(s <= g + 1e-12)


# ── Lautheit (vektorisiert) gegen pyloudnorm ────────────────────────────────

@pytest.mark.parametrize("signal", ["stereo", "mono", "leise_mit_pausen", "kurz"])
def test_lautheit_wie_pyloudnorm(signal):
    rng = np.random.default_rng(3)
    if signal == "stereo":
        x = rng.standard_normal((RATE * 20, 2)) * np.array([0.05, 0.02])
    elif signal == "mono":
        x = rng.standard_normal(RATE * 7 + 123) * 0.03
    elif signal == "leise_mit_pausen":
        x = rng.standard_normal((RATE * 30, 2)) * 0.05
        x[RATE * 5:RATE * 15] *= 1e-4                        # Pausen unter dem Gate
    else:
        x = rng.standard_normal((int(RATE * 0.9), 2)) * 0.05
    assert loudness._lautheit_lufs(x, RATE) == pytest.approx(_lufs(x), abs=1e-6)


def test_lautheit_mit_langer_digitaler_stille_schnell_und_richtig():
    import time
    rng = np.random.default_rng(4)
    x = np.zeros((RATE * 120, 2))
    x[:RATE * 10] = rng.standard_normal((RATE * 10, 2)) * 0.05     # 10 s Ton, dann Stille
    t = time.time()
    wert = loudness._lautheit_lufs(x, RATE)
    assert time.time() - t < 2.0
    assert wert == pytest.approx(_lufs(x), abs=1e-6)
