"""Lautheits-Normalisierung (EBU R128) und Fortschritts-Orchestrierung.

numpy/soundfile/pyloudnorm werden lazy in den Funktionen importiert.
"""
import os
import time
import logging

from punchbuddy.i18n import t
from punchbuddy.uikit import _dispatch_main, _show_progress_win


def _true_peak_db(data, block=1 << 18, rand=64):
    """True Peak in dBTP nach ITU-R BS.1770-4: 4-fach ueberabgetastet.

    Der reine Sample-Peak uebersieht Spitzen zwischen den Samples (bis ~3 dB).
    Blockweise, damit lange Beitraege nicht den vierfachen Speicher brauchen;
    `rand` Samples Ueberlappung, damit das Filter an den Blockgrenzen stimmt.
    """
    import numpy as np
    from scipy.signal import resample_poly

    n = len(data)
    peak = 0.0
    for start in range(0, n, block):
        a = max(0, start - rand)
        b = min(n, start + block + rand)
        up = resample_poly(data[a:b], 4, 1, axis=0)
        lo = (start - a) * 4
        hi = lo + (min(n, start + block) - start) * 4
        peak = max(peak, float(np.max(np.abs(up[lo:hi]))))
    return 20 * np.log10(peak) if peak > 0 else -120.0


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


def _spitzen_je_sample(data, block=1 << 18, rand=64):
    """Groesster Betrag je Sample, 4-fach ueberabgetastet, inkl. der Zwischenwerte
    zum vorigen und naechsten Sample, ueber alle Kanaele (float32).

    Einmal messen, dann fuer jeden Gain nur skalieren. Kanaele zusammen, damit
    beide gleich begrenzt werden und das Stereobild bleibt.
    """
    import numpy as np
    from scipy.signal import resample_poly

    x2d = data if data.ndim == 2 else data[:, None]
    n = len(x2d)
    out = np.empty(n, dtype=np.float32)
    for start in range(0, n, block):
        a = max(0, start - rand)
        b = min(n, start + block + rand)
        stop = min(n, start + block)
        up = np.abs(resample_poly(x2d[a:b], 4, 1, axis=0)).max(axis=1)
        lo = (start - a) * 4
        # je Sample die vier Werte bis zum naechsten Sample, dazu die des vorigen
        q = up[lo:lo + (stop - start) * 4].reshape(-1, 4).max(axis=1)
        vorher = up[lo - 4:lo].max() if lo >= 4 else 0.0
        out[start:stop] = np.maximum(q, np.concatenate(([vorher], q[:-1])))
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

    if spitzen is None:
        spitzen = _spitzen_je_sample(x)
    ceiling = 10 ** (max_truepeak / 20.0)
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


def _auf_ziel_mit_limiter(data, rate, meter, gain_db, target_lufs, max_truepeak, spitzen=None):
    """Gain auf die Ziel-Lautheit, Spitzen mit dem True-Peak-Limiter begrenzen.

    Der Limiter nimmt etwas Lautheit weg; deshalb nachstellen, bis die Lautheit
    hoechstens LAUTHEIT_TOLERANZ_LU vom Ziel abweicht (bis zu drei Durchgaenge).
    `spitzen`: _spitzen_je_sample(data), falls schon gemessen.
    Gibt (Signal, Gain dB, groesste Begrenzung dB, Lautheit LUFS, True Peak dBTP) zurueck.
    """
    if spitzen is None:
        spitzen = _spitzen_je_sample(data)
    for versuch in range(3):
        faktor = 10 ** (gain_db / 20.0)
        out, gr_db = _true_peak_limiter(data * faktor, rate, max_truepeak, spitzen * faktor)
        lufs = meter.integrated_loudness(out)
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
        import pyloudnorm as pyln
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
    meter = pyln.Meter(rate)
    current_lufs = meter.integrated_loudness(data)
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
    spitzen = _spitzen_je_sample(data)
    original_peak_db = float(20 * np.log10(spitzen.max())) if spitzen.max() > 0 else -120.0
    peak_db = original_peak_db + gain_db
    logging.info(f"  True Peak nach Gain: {peak_db:.1f} dBTP (Max: {max_truepeak} dBTP)")

    limiter_db = 0.0
    if peak_db > max_truepeak:
        # True-Peak-Limiter: nur die Spitzen werden begrenzt, die Lautheit bleibt
        # beim Ziel (frueher wurde die ganze Spur abgesenkt).
        normalized, gain_db, limiter_db, final_lufs_val, final_peak_db = _auf_ziel_mit_limiter(
            data, rate, meter, gain_db, target_lufs, max_truepeak, spitzen)
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

