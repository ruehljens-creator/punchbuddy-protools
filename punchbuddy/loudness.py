"""Lautheits-Normalisierung (EBU R128) und Fortschritts-Orchestrierung.

numpy/soundfile/pyloudnorm werden lazy in den Funktionen importiert.
"""
import os
import time
import logging

from punchbuddy.i18n import t
from punchbuddy.uikit import _dispatch_main, _show_progress_win


# ITU-R BS.1770-4, Anhang 2: Interpolationsfilter der 4-fachen Ueberabtastung fuer
# den True Peak (48 Taps = 4 Phasen zu je 12 Koeffizienten).
_TP_PHASEN = (
    (0.0017089843750, 0.0109863281250, -0.0196533203125, 0.0332031250000, -0.0594482421875,
     0.1373291015625, 0.9721679687500, -0.1022949218750, 0.0476074218750, -0.0266113281250,
     0.0148925781250, -0.0083007812500),
    (-0.0291748046875, 0.0292968750000, -0.0517578125000, 0.0891113281250, -0.1665039062500,
     0.4650878906250, 0.7797851562500, -0.2003173828125, 0.1015625000000, -0.0582275390625,
     0.0330810546875, -0.0189208984375),
    (-0.0189208984375, 0.0330810546875, -0.0582275390625, 0.1015625000000, -0.2003173828125,
     0.7797851562500, 0.4650878906250, -0.1665039062500, 0.0891113281250, -0.0517578125000,
     0.0292968750000, -0.0291748046875),
    (-0.0083007812500, 0.0148925781250, -0.0266113281250, 0.0476074218750, -0.1022949218750,
     0.9721679687500, 0.1373291015625, -0.0594482421875, 0.0332031250000, -0.0196533203125,
     0.0109863281250, 0.0017089843750),
)
_TP_BLOCK = 1 << 16    # Samples je Block
_TP_RAND = 16          # Ueberlappung; das Filter reicht 12 Samples weit


def _tp_filter():
    """(Filter verschachtelt fuer upfirdn, groesste Ueberhoehung zwischen Samples ~2,03)."""
    import numpy as np
    ph = np.array(_TP_PHASEN, dtype=np.float32)
    return ph.T.reshape(-1).copy(), float(np.abs(ph).sum(axis=1).max())


def _bloecke(n):
    return [(s, min(n, s + _TP_BLOCK)) for s in range(0, n, _TP_BLOCK)]


def _parallel(fn, items):
    """fn ueber items auf mehreren Kernen; numpy/scipy geben dabei den GIL frei."""
    if len(items) < 2:
        return [fn(i) for i in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as ex:
        return list(ex.map(fn, items))


def _spitzen_block(x2d, a, b, h):
    """Je Sample in [a, b): groesster ueberabgetasteter Betrag zwischen voriger und
    naechster Probe, ueber alle Kanaele."""
    import numpy as np
    from scipy.signal import upfirdn
    lo, hi = max(0, a - _TP_RAND), min(len(x2d), b + _TP_RAND)
    up = np.abs(upfirdn(h, x2d[lo:hi].astype(np.float32), up=4, axis=0)).max(axis=1)
    # upfirdn verzoegert um (48-1)/2 = 23,5 Werte: Sample s liegt bei 4*(s-lo)+23,5.
    # Werte 4*(s-lo)+20 ... +27 decken s-1 bis s+1 ab.
    base = 4 * (a - lo) + 20
    q = up[base:base + 4 * (b - a + 1)].reshape(-1, 4).max(axis=1)
    return np.maximum(q[:-1], q[1:])


def _true_peak_db(data):
    """True Peak in dBTP nach ITU-R BS.1770-4 (4-fach ueberabgetastet, Filter aus Anhang 2).

    Schnell: Ein Block kann den True Peak nur bestimmen, wenn sein Sample-Peak mal der
    groessten Ueberhoehung des Filters den hoechsten Sample-Peak uebersteigt; nur diese
    Bloecke werden ueberabgetastet, parallel auf mehreren Kernen.
    """
    import numpy as np
    x2d = data if data.ndim == 2 else data[:, None]
    h, ueberhoehung = _tp_filter()
    bloecke = _bloecke(len(x2d))
    bm = [float(np.abs(x2d[a:b]).max()) for a, b in bloecke]
    smax = max(bm, default=0.0)
    if smax <= 0:
        return -120.0
    kandidaten = [bl for bl, m in zip(bloecke, bm) if m * ueberhoehung > smax]
    peaks = _parallel(lambda bl: float(_spitzen_block(x2d, bl[0], bl[1], h).max()), kandidaten)
    return float(20 * np.log10(max([smax] + peaks)))


def _lautheit_lufs(data, rate):
    """Integrierte Lautheit nach ITU-R BS.1770-4 in LUFS.

    Gleiche Filter (aus pyloudnorm.Meter) und gleiches Gating wie
    pyloudnorm.Meter.integrated_loudness, aber vektorisiert (Summen ueber
    kumulierte Quadrate statt Schleife je Block) und in Stuecken parallel.
    """
    import numpy as np
    import pyloudnorm as pyln
    from scipy.signal import lfilter

    x2d = data if data.ndim == 2 else data[:, None]
    meter = pyln.Meter(rate)
    stufen = list(meter._filters.values())          # Hochregal, dann Hochpass
    g = np.array([1.0, 1.0, 1.0, 1.41, 1.41])[:x2d.shape[1]]

    n = len(x2d)
    # Die Filter schwingen nach weniger als 1 s vollstaendig ein (Pole bei r ~ 0,995):
    # Stuecke mit 1 s Vorlauf lassen sich deshalb parallel filtern, das Ergebnis ist
    # bis auf Rundung gleich.
    vorlauf = int(rate)
    stueck = max(vorlauf * 10, -(-n // 8))
    auftraege = [(i, a, min(n, a + stueck)) for i in range(x2d.shape[1]) for a in range(0, n, stueck)]
    quadrate = np.empty((n, x2d.shape[1]), dtype=np.float64)

    def filtern(auftrag):
        i, a, b = auftrag
        v = max(0, a - vorlauf)
        y = np.array(x2d[v:b, i], dtype=np.float64)        # Kopie, Original bleibt
        # Gegen denormale Zahlen: in digitaler Stille (z. B. bis zum Videoende aufgefuellt)
        # klingen die IIR-Filter in winzige Werte aus, die auf Intel-CPUs bis zu 20-mal
        # langsamer rechnen. Ein Teppich von +-1e-20 (etwa -400 dBFS, faellt unter das
        # Gate) haelt die Werte im normalen Zahlenbereich.
        y[(v % 2)::2] += 1e-20
        y[1 - (v % 2)::2] -= 1e-20
        for f in stufen:
            y = f.passband_gain * lfilter(f.b, f.a, y)
        quadrate[a:b, i] = y[a - v:] ** 2

    _parallel(filtern, auftraege)
    cs = [np.concatenate(([0.0], np.cumsum(quadrate[:, i]))) for i in range(x2d.shape[1])]
    t_g, step = meter.block_size, 1.0 - meter.overlap
    anzahl = int(np.round(((n / rate - t_g) / (t_g * step)))) + 1
    j = np.arange(anzahl)
    lo = np.minimum((t_g * (j * step) * rate).astype(np.int64), n)
    hi = np.minimum((t_g * (j * step + 1) * rate).astype(np.int64), n)
    z = np.array([(c[hi] - c[lo]) / (t_g * rate) for c in cs])
    with np.errstate(divide="ignore", invalid="ignore"):
        lj = -0.691 + 10.0 * np.log10((g[:, None] * z).sum(axis=0))
        auswahl = lj >= -70.0
        if not auswahl.any():
            return float("-inf")
        relativ = -0.691 + 10.0 * np.log10((g * z[:, auswahl].mean(axis=1)).sum()) - 10.0
        auswahl = (lj > relativ) & (lj > -70.0)
        mittel = np.nan_to_num(z[:, auswahl].mean(axis=1)) if auswahl.any() else np.zeros(len(g))
        return float(-0.691 + 10.0 * np.log10((g * mittel).sum()))


# ─────────────────────────────────────────────────────────────────────────────
# True-Peak-Limiter (offline, mit Vorschau)
# ─────────────────────────────────────────────────────────────────────────────
LIMITER_LOOKAHEAD_S = 0.002   # Vorschau = Einschwingzeit der Begrenzung
LIMITER_RELEASE_S = 0.100     # Rueckstellzeit nach einer Spitze
# Grenze genau bei max_truepeak (Vorgabe Jens: -3). True Peak begrenzt auch den Sample-Peak.
# Erlaubt sind je 0,1 dB Abweichung bei Spitzen und Lautheit (Vorgabe Jens).
_LIMITER_BLOCK = 16           # Raster fuer die Rueckstellung (Samples)
LAUTHEIT_TOLERANZ_LU = 0.1    # erlaubte Abweichung der Lautheit vom Ziel
TRUE_PEAK_TOLERANZ_DB = 0.1   # erlaubte Ueberschreitung der Spitzengrenze


def _spitzen_je_sample(data, unter=None):
    """Groesster Betrag je Sample, 4-fach ueberabgetastet, inkl. der Zwischenwerte
    zum vorigen und naechsten Sample, ueber alle Kanaele (float32).

    Einmal messen, dann fuer jeden Gain nur skalieren. Kanaele zusammen, damit
    beide gleich begrenzt werden und das Stereobild bleibt.
    `unter`: Bloecke, die selbst ueberhoeht nicht ueber diesen Wert kommen, werden
    nicht ueberabgetastet (dort stehen die Sample-Peaks) – sie loesen nie aus.
    """
    import numpy as np
    x2d = data if data.ndim == 2 else data[:, None]
    h, ueberhoehung = _tp_filter()
    out = np.empty(len(x2d), dtype=np.float32)
    noetig = []
    for a, b in _bloecke(len(x2d)):
        sample = np.abs(x2d[a:b]).max(axis=1)
        if unter is not None and float(sample.max()) * ueberhoehung <= unter:
            out[a:b] = sample
        else:
            noetig.append((a, b))

    def rechnen(bl):
        out[bl[0]:bl[1]] = _spitzen_block(x2d, bl[0], bl[1], h)
    _parallel(rechnen, noetig)
    return out


def _verstaerkungskurve(g, lookahead, alpha_block):
    """Glatte Verstaerkung, die an jedem Sample hoechstens `g` ist.

    1. Minimum ueber die naechsten `lookahead` Samples (Vorschau),
    2. Rueckstellung exponentiell im Raster von _LIMITER_BLOCK Samples,
    3. gleitender Mittelwert ueber `lookahead` Samples (sanfter Einsatz).
    Da jedes Glied des Mittelwerts schon das Minimum ueber die Spitze enthaelt,
    liegt das Ergebnis nie ueber `g`.
    """
    import numpy as np
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    m = minimum_filter1d(g, size=lookahead, origin=-(lookahead // 2), mode="constant", cval=1.0)
    nb = -(-len(m) // _LIMITER_BLOCK)
    mb = np.ones(nb * _LIMITER_BLOCK)
    mb[:len(m)] = m
    mb = mb.reshape(nb, _LIMITER_BLOCK).min(axis=1)
    # Rueckstellung r[j] = min(mb[j], r[j-1] + (1 - r[j-1]) * alpha) ohne Schleife:
    # mit d = 1 - r gilt d[j] = max(u[j], beta * d[j-1]), also
    # d[j] = beta^j * max_{k<=j}(u[k] / beta^k) – im Logarithmus gerechnet.
    u = 1.0 - mb
    lb = np.log(1.0 - alpha_block)
    k = np.arange(nb) * lb
    with np.errstate(divide="ignore"):
        d = np.exp(k + np.maximum.accumulate(np.log(u) - k))
    rb = 1.0 - np.maximum(d, u)
    r = np.repeat(rb, _LIMITER_BLOCK)[:len(m)]
    # Vor dem Anfang gilt der erste Wert: steht eine Spitze in den ersten Samples,
    # greift die Begrenzung dort sofort (keine Vorschau vor dem Dateianfang).
    return uniform_filter1d(r, size=lookahead, origin=(lookahead - 1) // 2, mode="nearest")


def _true_peak_limiter(x, rate, max_truepeak, spitzen=None):
    """Begrenzt den True Peak auf `max_truepeak`.

    `spitzen`: Ergebnis von _spitzen_je_sample(x), falls schon gemessen.
    Gibt (Signal, groesste Begrenzung in dB) zurueck. Bearbeitet nur die
    Bereiche um die Spitzen; der Rest bleibt bitgleich.
    """
    import numpy as np

    ceiling = 10 ** (max_truepeak / 20.0)
    if spitzen is None:
        spitzen = _spitzen_je_sample(x, unter=ceiling)
    idx = np.nonzero(spitzen > ceiling)[0]
    if len(idx) == 0:
        return x, 0.0
    faktor = ceiling / spitzen[idx].astype(np.float64)

    x2d = x if x.ndim == 2 else x[:, None]
    n = len(x2d)
    lookahead = max(2, int(round(LIMITER_LOOKAHEAD_S * rate)))
    alpha_block = 1.0 - np.exp(-_LIMITER_BLOCK / (LIMITER_RELEASE_S * rate))
    nachlauf = int(LIMITER_RELEASE_S * rate * np.log(1e5))  # Rest < 1e-5

    # Bereiche um die Ueberschreitungen; ueberlappende zusammenfassen
    anf = np.maximum(idx - 2 * lookahead, 0)
    end = np.minimum(idx + lookahead + nachlauf, n)
    neu_ab = np.concatenate(([True], anf[1:] > np.maximum.accumulate(end)[:-1]))
    starts = anf[neu_ab]
    stops = np.maximum.reduceat(end, np.nonzero(neu_ab)[0])

    y = x2d.copy()
    tiefste = 1.0
    for a, b in zip(starts, stops):
        g = np.ones(b - a)
        lo, hi = np.searchsorted(idx, [a, b])
        g[idx[lo:hi] - a] = faktor[lo:hi]
        s = _verstaerkungskurve(g, lookahead, alpha_block)
        y[a:b] *= s[:, None]
        tiefste = min(tiefste, float(s.min()))
    gr_db = -20 * np.log10(tiefste)
    return (y if x.ndim == 2 else y[:, 0]), gr_db


def _auf_ziel_mit_limiter(data, rate, gain_db, target_lufs, max_truepeak, spitzen=None):
    """Gain auf die Ziel-Lautheit, Spitzen mit dem True-Peak-Limiter begrenzen.

    Der Limiter nimmt etwas Lautheit weg; deshalb nachstellen, bis die Lautheit
    hoechstens LAUTHEIT_TOLERANZ_LU vom Ziel abweicht (bis zu drei Durchgaenge).
    `spitzen`: _spitzen_je_sample(data) vollstaendig gemessen, sonst wird nur gemessen,
    was die Grenze erreichen kann.
    Gibt (Signal, Gain dB, groesste Begrenzung dB, Lautheit LUFS, True Peak dBTP) zurueck.
    """
    ceiling = 10 ** (max_truepeak / 20.0)
    reserve = 10 ** (1.0 / 20.0)    # Nachstellen hebt den Gain selten um mehr als 1 dB
    gilt_bis = float("inf") if spitzen is not None else 0.0
    for versuch in range(3):
        faktor = 10 ** (gain_db / 20.0)
        if faktor > gilt_bis:
            # nur Bloecke ueberabtasten, die bei diesem Gain (+1 dB) die Grenze erreichen koennen
            gilt_bis = faktor * reserve
            spitzen = _spitzen_je_sample(data, unter=ceiling / gilt_bis)
        out, gr_db = _true_peak_limiter(data * faktor, rate, max_truepeak, spitzen * faktor)
        lufs = _lautheit_lufs(out, rate)
        if abs(target_lufs - lufs) <= LAUTHEIT_TOLERANZ_LU or versuch == 2:
            break
        gain_db += target_lufs - lufs
    # Absicherung: liegt der True Peak mehr als die Toleranz ueber der Grenze,
    # statisch auf die Grenze absenken (sollte nicht vorkommen)
    tp = _true_peak_db(out)
    if tp > max_truepeak + TRUE_PEAK_TOLERANZ_DB:
        logging.warning(f"  True Peak nach Limiter {tp - max_truepeak:.2f} dB zu hoch – statische Absenkung")
        out = out * 10 ** ((max_truepeak - tp) / 20.0)
        lufs -= tp - max_truepeak
        tp = max_truepeak
    return out, gain_db, gr_db, lufs, tp


def normalize_track(engine, session_dir, track_name="ST", target_lufs=-23.0, max_truepeak=-3.0, progress_cb=None):
    """
    Normalisiert die konsolidierte Audiodatei einer Spur nach EBU R128.
    1. Findet die neueste '<track_name>*' .wav im Audio Files Ordner
    2. Misst integrierte Lautheit (LUFS) und True Peak
    3. Wendet Gain-Korrektur an (mit True-Peak-Limiter)
    4. Ueberschreibt die Datei
    5. Aktualisiert Pro Tools (refresh)
    """
    def _prog(frac, msg):
        if progress_cb:
            try: progress_cb(frac, msg)
            except Exception: pass

    logging.info(f"  Loudness-Korrektur fuer Spur '{track_name}'...")
    _prog(0.05, t("prog_track_search").format(track_name))
    try:
        import soundfile as sf
        import pyloudnorm  # noqa: F401 – nur pruefen, ob vorhanden (Filter fuer _lautheit_lufs)
        import numpy as np
    except ImportError as e:
        logging.error(f"Normalisierung: fehlende Bibliothek: {e}")
        logging.error("  pip3 install pyloudnorm soundfile")
        return

    audio_dir = os.path.join(session_dir, "Audio Files")
    if not os.path.isdir(audio_dir):
        logging.error(f"  Audio-Ordner nicht gefunden: {audio_dir}")
        return

    # Neueste konsolidierte Datei fuer diese Spur finden
    st_files = []
    for f in os.listdir(audio_dir):
        base = os.path.splitext(f)[0]
        if (base == track_name or base.startswith(track_name + "_") or base.startswith(track_name + ".") or base.startswith(track_name + "-")) and f.lower().endswith(".wav"):
            full = os.path.join(audio_dir, f)
            mtime = os.path.getmtime(full)
            size = os.path.getsize(full)
            st_files.append((mtime, size, full, f))

    if not st_files:
        logging.warning(f"  Keine {track_name}*.wav Dateien gefunden – Normalisierung uebersprungen.")
        return

    st_files.sort(reverse=True)  # Neueste zuerst (nach mtime)
    target_file = st_files[0][2]
    target_name = st_files[0][3]
    logging.info(f"  Datei: {target_name} ({st_files[0][1] / 1024 / 1024:.1f} MB)")

    # Split-Mono (Session nicht interleaved): Pro Tools legt je Kanal eine Datei an,
    # z. B. ST_02.L.wav und ST_02.R.wav. Beide gehoeren zusammen: gemeinsam messen,
    # gleicher Gain, beide schreiben. Sonst wird nur ein Kanal korrigiert.
    paar = None
    stamm, kanal = os.path.splitext(os.path.splitext(target_name)[0])
    if kanal in (".L", ".R"):
        ext = os.path.splitext(target_name)[1]
        links = os.path.join(audio_dir, stamm + ".L" + ext)
        rechts = os.path.join(audio_dir, stamm + ".R" + ext)
        if os.path.exists(links) and os.path.exists(rechts):
            paar = (links, rechts)
    quelle = f"{stamm}.L{ext} + {stamm}.R{ext}" if paar else target_name

    # Audio lesen
    _prog(0.15, t("prog_track_read").format(target_name))
    if paar:
        data_l, rate = sf.read(paar[0])
        data_r, rate_r = sf.read(paar[1])
        if rate_r != rate or len(data_l) != len(data_r):
            logging.error(f"  Split-Mono-Paar {quelle} passt nicht zusammen – Normalisierung uebersprungen.")
            return
        data = np.column_stack([data_l, data_r])
        logging.info(f"  Split-Mono-Paar: {quelle}")
    else:
        data, rate = sf.read(target_file)
    logging.info(f"  Sample-Rate: {rate} Hz, Dauer: {len(data)/rate:.1f}s, Kanaele: {data.ndim}")

    # Lautheit messen
    _prog(0.35, t("prog_track_measure").format(target_name))
    current_lufs = _lautheit_lufs(data, rate)
    logging.info(f"  Aktuelle Lautheit: {current_lufs:.1f} LUFS (Ziel: {target_lufs} LUFS)")

    if current_lufs == float('-inf'):
        logging.warning("  Stille erkannt – Normalisierung uebersprungen.")
        return

    # Gain berechnen
    gain_db = target_lufs - current_lufs
    gain_linear = 10 ** (gain_db / 20.0)
    logging.info(f"  Gain-Korrektur: {gain_db:+.1f} dB")

    # Gain anwenden
    _prog(0.55, t("prog_track_gain").format(target_name, gain_db))
    normalized = data * gain_linear

    # True Peak pruefen und limitieren. Einmal am Original messen; der True Peak
    # waechst linear mit dem Gain.
    original_peak_db = _true_peak_db(data)
    peak_db = original_peak_db + gain_db
    logging.info(f"  True Peak nach Gain: {peak_db:.1f} dBTP (Max: {max_truepeak} dBTP)")

    limiter_db = 0.0
    if peak_db > max_truepeak:
        # True-Peak-Limiter: nur die Spitzen werden begrenzt, die Lautheit bleibt
        # beim Ziel (frueher wurde die ganze Spur abgesenkt).
        normalized, gain_db, limiter_db, final_lufs_val, final_peak_db = _auf_ziel_mit_limiter(
            data, rate, gain_db, target_lufs, max_truepeak)
        logging.info(f"  True Peak Limiter: hoechstens {limiter_db:.1f} dB Begrenzung, Gain {gain_db:+.1f} dB")
    else:
        logging.info("  True Peak OK – kein Limiting noetig")

    # Datei ueberschreiben – immer 24 bit (Abgabeformat 24 bit / 48 kHz).
    # Ohne subtype schreibt soundfile PCM_16. Die Samplerate bleibt die der Session,
    # sonst passt die Datei nicht mehr zur Session.
    if rate != 48000:
        logging.warning(f"  Samplerate {rate} Hz statt 48000 Hz – Abgabeformat ist 24 bit / 48 kHz")
    _prog(0.70, t("prog_track_write").format(target_name))
    if paar:
        sf.write(paar[0], normalized[:, 0], rate, subtype="PCM_24")
        sf.write(paar[1], normalized[:, 1], rate, subtype="PCM_24")
    else:
        sf.write(target_file, normalized, rate, subtype="PCM_24")
    logging.info(f"  Datei ueberschrieben: {quelle} (24 bit)")

    # ── Loudness Correction Metadata schreiben ───────────────────────
    limiting_applied = limiter_db > 0
    if not limiting_applied:
        final_peak_db, final_lufs_val = peak_db, target_lufs

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    duration_s = len(data) / rate
    duration_min = int(duration_s // 60)
    duration_sec = duration_s % 60

    meta_path = os.path.join(session_dir, "Loudness Correction Metadata.txt")
    try:
        with open(meta_path, "w", encoding="utf-8") as mf:
            mf.write("=" * 60 + "\n")
            mf.write("  LOUDNESS CORRECTION METADATA\n")
            mf.write("  EBU R128 / ITU-R BS.1770\n")
            mf.write("=" * 60 + "\n\n")
            mf.write(f"  Datum:              {timestamp}\n")
            mf.write(f"  Quelldatei:         {quelle}\n")
            mf.write(f"  Sample-Rate:        {rate} Hz\n")
            mf.write("  Format:             24 bit PCM\n")
            mf.write(f"  Kanaele:            {'Stereo' if data.ndim == 2 else 'Mono'}\n")
            mf.write(f"  Dauer:              {duration_min}:{duration_sec:05.2f}\n\n")
            mf.write("-" * 60 + "\n")
            mf.write("  MESSWERTE\n")
            mf.write("-" * 60 + "\n\n")
            mf.write(f"  Original Lautheit:  {current_lufs:.1f} LUFS\n")
            mf.write(f"  Ziel Lautheit:      {target_lufs:.1f} LUFS\n")
            mf.write(f"  Gain-Korrektur:     {gain_db:+.1f} dB\n\n")
            mf.write(f"  Original True Peak: {original_peak_db:.1f} dBTP\n")
            mf.write(f"  Max True Peak:      {max_truepeak:.1f} dB\n")
            mf.write(f"  True Peak Limiter:  {'Ja (hoechstens %.1f dB Begrenzung)' % limiter_db if limiting_applied else 'Nein'}\n\n")
            mf.write("-" * 60 + "\n")
            mf.write("  ERGEBNIS\n")
            mf.write("-" * 60 + "\n\n")
            mf.write(f"  Endgueltige Lautheit: {final_lufs_val:.1f} LUFS\n")
            mf.write(f"  Endgueltiger Peak:    {final_peak_db:.1f} dB TP\n")
            konform = (abs(final_lufs_val - target_lufs) <= LAUTHEIT_TOLERANZ_LU
                       and final_peak_db <= max_truepeak + TRUE_PEAK_TOLERANZ_DB)
            mf.write(f"  Norm konform:         {'JA' if konform else 'NEIN'} (Toleranz je 0,1 dB)\n\n")
            mf.write("=" * 60 + "\n")
        logging.info(f"  Metadata geschrieben: {meta_path}")
    except Exception as e:
        logging.warning(f"  Metadata schreiben: {e}")

    # Pro Tools aktualisieren und Clip umbenennen
    _prog(0.85, t("prog_track_refresh").format(target_name))
    try:
        # Puffer fuer OS-Datei-Schreibvorgaenge und PT-Hintergrund-Tasks
        time.sleep(1.5)
        try:
            engine.refresh_all_modified_audio_files()
            logging.info("  Pro Tools Audio-Dateien aktualisiert")
        except Exception as re:
            logging.warning(f"  Pro Tools Audio-Dateien Refresh fehlgeschlagen (wird fortgesetzt): {re}")
        time.sleep(3.0)  # PT braucht Zeit um die Datei neu einzulesen

        # Clip-Name = Dateiname ohne Extension (z.B. ST_02.wav -> ST_02),
        # bei Split-Mono ohne Kanalendung (ST_02.L.wav -> ST_02)
        clip_name = stamm if paar else os.path.splitext(target_name)[0]

        # Bereits umbenannt? (verhindert ST_02-loudness -> ST_02-loudness-loudness)
        if "-loudness" in clip_name:
            logging.info(f"  Clip '{clip_name}' hat bereits '-loudness' Suffix – Umbenennung uebersprungen.")
        else:
            new_name = f"{clip_name}-loudness"
            renamed = False

            # Versuch 1: rename_target_clip mit exaktem Clip-Namen (Dateiname ohne Extension)
            for rf in [True, False]:
                try:
                    engine.rename_target_clip(clip_name, new_name, rename_file=rf)
                    logging.info(f"  Clip umbenannt: {clip_name} -> {new_name} (rename_file={rf})")
                    renamed = True
                    break
                except Exception:
                    continue

            # Versuch 2: Reiner Track-Name (typisch fuer Stereo Interleaved nach Consolidate)
            if not renamed and clip_name != track_name:
                for rf in [True, False]:
                    try:
                        engine.rename_target_clip(track_name, f"{track_name}-loudness", rename_file=rf)
                        logging.info(f"  Clip umbenannt (Track-Name): {track_name} -> {track_name}-loudness (rename_file={rf})")
                        renamed = True
                        new_name = f"{track_name}-loudness"
                        break
                    except Exception:
                        continue

            # Versuch 3: Nummerierte Fallbacks (<track_name>_01, _02, ...)
            if not renamed:
                for i in range(1, 20):
                    try_name = f"{track_name}_{i:02d}"
                    for rf in [True, False]:
                        try:
                            engine.rename_target_clip(try_name, f"{try_name}-loudness", rename_file=rf)
                            logging.info(f"  Clip umbenannt (Fallback): {try_name} -> {try_name}-loudness (rename_file={rf})")
                            renamed = True
                            new_name = f"{try_name}-loudness"
                            break
                        except Exception:
                            continue
                    if renamed:
                        break

            if not renamed:
                logging.warning("  Clip konnte nicht umbenannt werden")
            else:
                # ── Timeline-Clip Rename Absicherung ──────────────────────────
                # Da der Clip auf der Timeline nach dem Trimmen ein Sub-Clip ist,
                # benennt rename_target_clip oft nur das File/Hauptclip um.
                # Wir selektieren den Clip auf der Spur und benennen ihn explizit um.
                try:
                    engine.select_all_clips_on_track(track_name)
                    time.sleep(0.25)
                    engine.rename_selected_clip(new_name, rename_file=False)
                    logging.info(f"  Timeline-Clip auf Spur '{track_name}' umbenannt -> {new_name}")
                except Exception as e:
                    logging.warning(f"  Timeline-Clip Rename fehlgeschlagen auf Spur '{track_name}': {e}")

            # ── Datei-Rename Absicherung ──────────────────────────────────
            # PT aendert manchmal nur den Clip-Namen intern, benennt aber die
            # Datei auf der Festplatte nicht um (besonders bei Stereo Interleaved).
            # Wir pruefen ob die Datei noch den alten Namen hat und benennen sie
            # manuell um, damit PT den korrekten Namen auf der Spur anzeigt.
            # Nicht bei Split-Mono: dort muessten beide Dateien zusammen umbenannt
            # werden; das ist mit Pro Tools nicht erprobt.
            if renamed and paar:
                logging.info("  Split-Mono: Dateien werden nicht von Hand umbenannt.")
            elif renamed and os.path.exists(target_file):
                new_file = os.path.join(os.path.dirname(target_file),
                                        new_name + os.path.splitext(target_name)[1])
                if not os.path.exists(new_file):
                    try:
                        os.rename(target_file, new_file)
                        logging.info(f"  Datei manuell umbenannt: {target_name} -> {os.path.basename(new_file)}")
                        # Puffer vor dem Refresh
                        time.sleep(0.5)
                        try:
                            engine.refresh_all_modified_audio_files()
                        except Exception as re:
                            logging.warning(f"  Pro Tools Audio-Dateien Refresh nach manuellem Rename fehlgeschlagen: {re}")
                        time.sleep(1.5)
                    except OSError as e:
                        logging.warning(f"  Datei-Rename fehlgeschlagen: {e}")
                else:
                    logging.info(f"  Datei bereits umbenannt: {os.path.basename(new_file)}")
    except Exception as e:
        logging.warning(f"  PT Rename/Refresh Hauptfehler: {e}")

    _prog(0.98, f"Spur '{track_name}': Fertig.")


# ─────────────────────────────────────────────────────────────────────────────
# Lautheits-Fortschrittsfenster
# ─────────────────────────────────────────────────────────────────────────────

_loudness_win_refs = []  # Hält ObjC-Referenzen am Leben (verhindert PyObjC-Dealloc-Crash)


def _run_loudness_with_progress(engine, session_dir, loud_tracks, target_lufs, max_tp):
    """Ruft normalize_track für jede Spur auf und zeigt dabei ein Fortschrittsfenster."""
    import AppKit as _AK

    win_ref   = [None]
    bar_ref   = [None]
    phase_ref = [None]
    WIN_W, WIN_H = 360, 100

    def _make_window():
        try:
            rect  = _AK.NSMakeRect(0, 0, WIN_W, WIN_H)
            win   = _AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, _AK.NSWindowStyleMaskTitled, _AK.NSBackingStoreBuffered, False)
            win.setTitle_(t("prog_loudness_win_title"))
            win.setLevel_(3)
            win.center()

            cv = win.contentView()

            lbl = _AK.NSTextField.alloc().initWithFrame_(
                _AK.NSMakeRect(20, WIN_H - 38, WIN_W - 40, 18))
            lbl.setStringValue_(t("prog_loudness_init"))
            lbl.setBezeled_(False)
            lbl.setEditable_(False)
            lbl.setDrawsBackground_(False)
            lbl.setFont_(_AK.NSFont.systemFontOfSize_(12))
            cv.addSubview_(lbl)

            bar = _AK.NSProgressIndicator.alloc().initWithFrame_(
                _AK.NSMakeRect(20, WIN_H - 66, WIN_W - 40, 16))
            bar.setStyle_(0)  # 0 = NSProgressIndicatorBarStyle (Balken)
            bar.setIndeterminate_(False)
            bar.setMinValue_(0.0)
            bar.setMaxValue_(1.0)
            bar.setDoubleValue_(0.0)
            cv.addSubview_(bar)

            win_ref[0]   = win
            bar_ref[0]   = bar
            phase_ref[0] = lbl
            _loudness_win_refs.extend([win, bar, lbl])
            win.makeKeyAndOrderFront_(None)
        except Exception as e:
            logging.debug(f"  Loudness-Fortschrittsfenster: {e}")

    def _update(frac, msg):
        def _do():
            try:
                if bar_ref[0]:   bar_ref[0].setDoubleValue_(frac)
                if phase_ref[0]: phase_ref[0].setStringValue_(msg)
            except Exception:
                pass
        _dispatch_main(_do)

    def _close():
        def _do():
            try:
                if win_ref[0]:
                    win_ref[0].orderOut_(None)
                    win_ref[0] = None
            except Exception:
                pass
        _dispatch_main(_do)

    _dispatch_main(_make_window)
    time.sleep(0.15)

    n = max(len(loud_tracks), 1)
    for i, lt in enumerate(loud_tracks):
        base, span = i / n, 1.0 / n
        def _cb(frac, msg, _b=base, _s=span):
            _update(_b + frac * _s, msg)
        normalize_track(engine, session_dir, lt, target_lufs, max_tp, progress_cb=_cb)

    _update(1.0, t("prog_loudness_done"))
    time.sleep(0.8)
    _close()


# ─────────────────────────────────────────────────────────────────────────────
# Fortschrittsfenster für Import / Export
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# Globale Referenzliste für ObjC-Objekte (verhindert PyObjC Dealloc-Crash)
# Python-Attribut-Assignments auf ObjC-Proxies lösen Deallokationskaskaden
# aus die in PyObjC/Python 3.14 SIGBUS/SIGSEGV verursachen.
# Deshalb: NIEMALS ObjC-Objekte als Instanz-Attribute speichern/überschreiben.
# ─────────────────────────────────────────────────────────────────────────────
_config_refs = []  # Wird in _open_config_window befüllt

