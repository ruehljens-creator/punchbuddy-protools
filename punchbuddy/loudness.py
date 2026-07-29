"""Lautheits-Normalisierung (EBU R128) und Fortschritts-Orchestrierung.

numpy/soundfile/pyloudnorm werden lazy in den Funktionen importiert.
"""
import os
import time
import logging

from punchbuddy.i18n import t
from punchbuddy.uikit import _dispatch_main, _show_progress_win

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

    # Audio lesen
    _prog(0.15, t("prog_track_read").format(target_name))
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

    # True Peak pruefen und limitieren
    peak_linear = np.max(np.abs(normalized))
    peak_db = 20 * np.log10(peak_linear) if peak_linear > 0 else -120.0
    logging.info(f"  True Peak nach Gain: {peak_db:.1f} dB (Max: {max_truepeak} dB)")

    if peak_db > max_truepeak:
        # Limitieren: Gain so reduzieren dass True Peak eingehalten wird
        reduction_db = peak_db - max_truepeak
        reduction_linear = 10 ** (-reduction_db / 20.0)
        normalized *= reduction_linear
        final_lufs = current_lufs + gain_db - reduction_db
        logging.info(f"  True Peak Limiter: -{reduction_db:.1f} dB angewendet")
        logging.info(f"  Endgueltige Lautheit: {final_lufs:.1f} LUFS")
    else:
        logging.info(f"  True Peak OK – kein Limiting noetig")

    # Datei ueberschreiben
    _prog(0.70, t("prog_track_write").format(target_name))
    sf.write(target_file, normalized, rate)
    logging.info(f"  Datei ueberschrieben: {target_name}")

    # ── Loudness Correction Metadata schreiben ───────────────────────
    final_peak = np.max(np.abs(normalized))
    final_peak_db = 20 * np.log10(final_peak) if final_peak > 0 else -120.0
    original_peak = np.max(np.abs(data))
    original_peak_db = 20 * np.log10(original_peak) if original_peak > 0 else -120.0
    limiting_applied = peak_db > max_truepeak
    if limiting_applied:
        final_lufs_val = current_lufs + gain_db - (peak_db - max_truepeak)
    else:
        final_lufs_val = target_lufs

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
            mf.write(f"  Quelldatei:         {target_name}\n")
            mf.write(f"  Sample-Rate:        {rate} Hz\n")
            mf.write(f"  Kanaele:            {'Stereo' if data.ndim == 2 else 'Mono'}\n")
            mf.write(f"  Dauer:              {duration_min}:{duration_sec:05.2f}\n\n")
            mf.write("-" * 60 + "\n")
            mf.write("  MESSWERTE\n")
            mf.write("-" * 60 + "\n\n")
            mf.write(f"  Original Lautheit:  {current_lufs:.1f} LUFS\n")
            mf.write(f"  Ziel Lautheit:      {target_lufs:.1f} LUFS\n")
            mf.write(f"  Gain-Korrektur:     {gain_db:+.1f} dB\n\n")
            mf.write(f"  Original True Peak: {original_peak_db:.1f} dB\n")
            mf.write(f"  Max True Peak:      {max_truepeak:.1f} dB\n")
            mf.write(f"  True Peak Limiter:  {'Ja (%.1f dB)' % (peak_db - max_truepeak) if limiting_applied else 'Nein'}\n\n")
            mf.write("-" * 60 + "\n")
            mf.write("  ERGEBNIS\n")
            mf.write("-" * 60 + "\n\n")
            mf.write(f"  Endgueltige Lautheit: {final_lufs_val:.1f} LUFS\n")
            mf.write(f"  Endgueltiger Peak:    {final_peak_db:.1f} dB TP\n")
            mf.write(f"  Norm konform:         {'JA' if final_lufs_val >= target_lufs - 0.5 and final_peak_db <= max_truepeak else 'NEIN'}\n\n")
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

        # Clip-Name = Dateiname ohne Extension (z.B. ST_02.wav -> ST_02)
        clip_name = os.path.splitext(target_name)[0]

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
            if renamed and os.path.exists(target_file):
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

