"""Transport-Orchestrierung: Punch-In/Out, Play, Play-Custom, Stop, GoTo.

Importiert nach „unten" (state, config, keys, engine, uikit). Wird von der UI
und dem Webtrigger aufgerufen.
"""
import gc
import time
import logging
import threading

from punchbuddy import state
from punchbuddy.config import load_settings, DEFAULT_SETTINGS
from punchbuddy.uikit import _dispatch_main
from punchbuddy.keys import (
    _send_key, _send_key_to_app, _pt_pid, _VK_OE, _VK_K,
)
from punchbuddy.engine import (
    _get_engine, _ptsl_call, ensure_preroll_on, restore_preroll,
    refresh_session_tracks, _get_cached_track_names, _show_error,
    _close_engine, _invalidate_session_tracks, _reset_engine,
)

_stop_lock    = threading.Lock()  # verhindert gleichzeitige Stop-Aufrufe

# ── Anlauf-Schutzfenster ─────────────────────────────────────────────────────
# Nach einem echten Transport-Start (Play/Play-Custom/Record) werden Toggle-
# Drücke für diese Zeitspanne NICHT als Stop interpretiert. Grund (Salven-Test
# 2026-07-21): Der Anlauf dauert wegen der bimodalen PTSL-Latenz mehrere
# Sekunden; Bediener drücken nach („kam nicht an"), und der erste Druck NACH
# dem tatsächlichen Start stoppte die gerade angelaufene Wiedergabe/Aufnahme
# sofort wieder (Play→Stop→Play-Flattern). Der 400-ms-Debounce fängt
# menschliches Doppeldrücken (~500 ms Abstand) nicht ab.
TRANSPORT_START_GRACE = 3.0
_last_transport_start = 0.0

# Spiegelbildlich für die Stop-Seite: Ein Stop braucht PT-seitig mehrere
# Sekunden (Satellite-Handshake). Drückt der Bediener in dieser Zeit erneut
# „Stop", landet der Druck NACH dem echten Stopp und würde als Play-START
# interpretiert – die Wiedergabe/Aufnahme ginge ungewollt wieder los.
# Deshalb: nach einem Stop-Befehl werden Start-Drücke kurz verworfen.
# Das Fenster wird beim BESTÄTIGTEN Stopp aufgefrischt (run_stop-Ende): PT
# braucht studioseitig bis zu ~6s zum Stoppen – die Gefahrenzone für den
# „Nachdruck wird Play"-Effekt beginnt erst NACH dem echten Stopp.
# 2026-07-21: 2s → 1s reduziert (User-Wunsch, nachdem App-Nap-Fix die
# Zustellverzögerung beseitigt hat – Nachdrücke kommen jetzt zeitnah an).
TRANSPORT_STOP_GRACE = 1.0
_last_stop_request = 0.0

def _mark_transport_start():
    global _last_transport_start
    _last_transport_start = time.time()

def _mark_stop_request():
    global _last_stop_request
    _last_stop_request = time.time()

# Merkt sich, welche Spuren PunchBuddy zuletzt record-enabled hat. Beim
# Profilwechsel (Record A → Record B mit anderer Spurauswahl) werden damit
# die nicht mehr gewollten Spuren wieder abgewählt – vorher blieben sie
# scharf und landeten mit in der nächsten Aufnahme (Bug, entdeckt 2026-07-23).
# None = noch kein Record seit App-Start → echter Zustand wird einmalig per
# track_list (is_record_enabled) aus Pro Tools gelesen.
_pb_last_armed = None

def run_punch_in(target_tracks: list, monitor_tracks: list = None):
    # Recording-Skript: Input-Monitoring wird NICHT umgeschaltet – Pro Tools
    # übernimmt das selbst. (Die Play-Input-Logik in run_play bleibt davon
    # unberührt.) monitor_tracks bleibt nur für Aufrufer-Kompatibilität.
    _since_stop = time.time() - _last_stop_request
    if _since_stop < TRANSPORT_STOP_GRACE:
        logging.info(f"Record: Start verworfen – Stop wurde vor {_since_stop:.1f}s ausgelöst "
                     f"(Schutzfenster {TRANSPORT_STOP_GRACE:.0f}s gegen Doppeldruck).")
        return
    with state.running_lock:
        if state.running:
            logging.warning("Läuft bereits – Trigger ignoriert.")
            return
        state.running = True
    _set_busy(True)

    try:
        logging.info("=== PunchBuddy START ===")
        logging.info(f"Record-Spuren: {target_tracks}")

        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfügbar – Abbruch.")
            return

        # ── 1. Session-Spuren aus Cache – kein PTSL-Call wenn bereits bekannt ─
        pt_names = _get_cached_track_names(engine)
        if pt_names is None:
            logging.error("Konnte Session-Spuren nicht lesen (PTSL Timeout) – Abbruch.")
            return

        rec_want     = set(t for t in target_tracks if t in pt_names)

        logging.info(f"Session-Spuren (gecacht): {pt_names}")
        logging.info(f"Record SOLL:    {sorted(rec_want)}")

        if not rec_want:
            logging.warning("Keine Record-Spuren in der Session gefunden – Abbruch.")
            return

        # ══════════════════════════════════════════════════════════════════
        # VOR AUFNAHME – strikt sequentiell, mit Settling-Delays
        # ══════════════════════════════════════════════════════════════════

        # ── Schritt 1: Pre-Roll EIN ──────────────────────────────────────
        ensure_preroll_on(engine)
        time.sleep(0.2)  # CHANGE: Settling nach Pre-Roll setzen

        # ── Schritt 1b: Veraltete Record-Arms abwählen (Profilwechsel) ───
        # Record A → Record B: Spuren der vorigen Auswahl blieben scharf und
        # landeten mit in der neuen Aufnahme. Deshalb: alles, was (von uns
        # oder laut PT) record-enabled ist, aber NICHT zur aktuellen Auswahl
        # gehört, wird abgewählt.
        global _pb_last_armed
        stale = set()
        if _pb_last_armed is None:
            # Erster Record seit App-Start: echten Zustand aus PT lesen.
            ok_tl, _tl = _ptsl_call(engine.track_list, label="RecArmInventory", timeout=8.0)
            if ok_tl and _tl:
                try:
                    stale = {tr.name for tr in _tl
                             if getattr(getattr(tr, "track_attributes", None),
                                        "is_record_enabled", False)} - rec_want
                except Exception as e:
                    logging.warning(f"Record-Arm-Inventur nicht lesbar: {e}")
        else:
            stale = set(_pb_last_armed) - rec_want
        if stale:
            ok_dis, _ = _ptsl_call(engine.set_track_record_enable_state,
                                   sorted(stale), False,
                                   label="RecDisarmStale", timeout=15.0)
            if ok_dis:
                logging.info(f"Record Enable AUS (Profilwechsel): {sorted(stale)}")
            else:
                logging.warning(f"Abwählen alter Record-Spuren fehlgeschlagen: {sorted(stale)}")
            time.sleep(0.2)

        # ── Schritt 2: Record Enable (3 Versuche) ───────────────────────
        # Timeout 15s: NEXIS-Schreiblast kann set_track_record_enable_state 10-11s verzögern
        rec_ok = False
        for attempt in range(3):
            ok, _ = _ptsl_call(
                engine.set_track_record_enable_state, sorted(rec_want), True,
                label=f"RecEnable#{attempt+1}", timeout=15.0
            )
            if ok:
                logging.info(f"Record Enable AN: {sorted(rec_want)}")
                rec_ok = True
                break
            logging.warning(f"Record Enable FEHLER (Versuch {attempt+1}/3)")
            time.sleep(0.5 * (attempt + 1))
        if not rec_ok:
            logging.warning("Record Enable fehlgeschlagen – mache trotzdem weiter.")
        # Gemerkten Arm-Zustand aktualisieren: bei Erfolg = aktuelle Auswahl,
        # bei Fehlschlag None → nächster Record macht wieder die PT-Inventur.
        _pb_last_armed = set(rec_want) if rec_ok else None

        time.sleep(0.3)  # Settling – PT muss Rec-Enable verarbeiten

        # Recording: kein Input-Monitor-Umschalten (Pro Tools macht das selbst).

        time.sleep(0.5)  # Settling bevor Aufnahme startet

        # ── Schritt 4: Transport-Arm EIN + Aufnahme starten (PTSL) ─────────
        # toggle_record_enable (Rec-Arm) + toggle_play_state (Play) = Record+Play
        # Komplett via PTSL – kein F12 / CGEvent nötig, funktioniert unabhängig von Fokus.
        _transport_pre_armed = False
        ok_arm_chk, _pre_arm_state = _ptsl_call(
            engine.transport_armed, label="TransportArmedPre", timeout=5.0)
        if ok_arm_chk:
            _transport_pre_armed = bool(_pre_arm_state)
        if not _transport_pre_armed:
            _ptsl_call(engine.toggle_record_enable, label="RecordArm", timeout=5.0)
            logging.info("Transport Record-Arm: EIN gesetzt (PTSL).")
        else:
            logging.info("Transport war bereits record-armed.")
        time.sleep(0.1)
        _ptsl_call(engine.toggle_play_state, label="RecordStart", timeout=5.0)
        _mark_transport_start()
        logging.info("Record gestartet (PTSL: record_arm + play).")

        # ── Schritt 5: Warten bis Transport läuft (max 5s) ──────────────
        _transport_started = False
        for _ in range(100):
            ok_ts, ts_r = _ptsl_call(engine.transport_state, label="WaitStart", timeout=6.0)
            if not ok_ts:
                time.sleep(0.05)
                continue
            s = str(ts_r)
            if s in ("TS_TransportRecording", "TS_TransportPlaying",
                     "TS_TransportIsCued", "TS_TransportIsCuedForPreview"):
                logging.info(f"Transport aktiv: {s}")
                _transport_started = True
                break
            time.sleep(0.05)
        if not _transport_started:
            logging.warning("Transport nie gestartet!")

        # ── Schritt 6: Warten bis Transport gestoppt ────────────────────
        last = ""
        consecutive_failures = 0
        _transport_confirmed_stopped = False
        if _transport_started:
            for _ in range(2000):  # max 10 Minuten
                ok_ts, ts_r = _ptsl_call(engine.transport_state, label="WaitStop", timeout=8.0)
                # NEXIS kann transport_state verzögern – 8s Tolerance, Schwelle auf 10
                if not ok_ts:
                    consecutive_failures += 1
                    if consecutive_failures >= 10:
                        logging.warning("Transport-Polling: 10x Timeout – breche ab (Anti-Stau).")
                        # Nur DIESE (tote) Instanz verwerfen – kein Wegschliessen
                        # einer evtl. schon ersetzten Engine, die ein paralleler
                        # Worker noch benutzt (Race/Zombie-Schutz).
                        _reset_engine(stale=engine)
                        time.sleep(1.0)
                        engine = _get_engine()
                        break
                    time.sleep(0.5)
                    continue
                consecutive_failures = 0
                s = str(ts_r)
                if s != last:
                    logging.info(f"Transport: {s}")
                    last = s
                if s == "TS_TransportStopped":
                    logging.info("Transport gestoppt.")
                    _transport_confirmed_stopped = True
                    break
                if s == "TS_TransportIsStopping":
                    # PT schreibt Aufnahme auf NEXIS – PTSL-Last jetzt drastisch reduzieren.
                    # 80ms-Polling würde ~12 Anfragen/s erzeugen genau wenn PT die Dateien
                    # finalisiert, was zu Beachball/Crash führen kann.
                    time.sleep(3.0)
                else:
                    time.sleep(0.3)
            else:
                logging.warning("Transport-Timeout (10 Min).")

        # ══════════════════════════════════════════════════════════════════
        # NACH AUFNAHME – strikt sequentiell, mit Settling-Delays
        # CHANGE: Reihenfolge angepasst – erst Rec-Disable damit PT den Record-
        # Pfad sauber schliesst, dann Pre-Roll AUS, dann Input EIN.
        # ══════════════════════════════════════════════════════════════════

        if _transport_confirmed_stopped:

            time.sleep(0.2)  # CHANGE: Settling nach Stopp, PT muss Record-Pfad finalisieren

            # Schritt 7 (Track RecordDisable) entfernt:
            # Tracks bleiben nach der Aufnahme armed (blau/punch-ready).
            # Das entspricht dem normalen TrackPunch-Workflow und verhindert
            # den NEXIS Cold-Start beim nächsten Record-Trigger nach Idle.

            time.sleep(0.2)  # Settling vor Transport-Arm-Änderung

            # ── Schritt 7b: Transport-Arm AUS (nur wenn wir ihn aktiviert haben) ──
            if not _transport_pre_armed:
                ok_arm2, still_armed = _ptsl_call(
                    engine.transport_armed, label="TransportArmedPost", timeout=5.0)
                if ok_arm2 and still_armed:
                    _ptsl_call(engine.toggle_record_enable, label="RecordDisarm", timeout=5.0)
                    logging.info("Transport Record-Arm: AUS gesetzt.")

            # ── Schritt 8: Pre-Roll AUS ──────────────────────────────────
            restore_preroll(engine)
            time.sleep(0.2)  # Settling nach Pre-Roll-Aus
            # Recording: kein Input-Monitor-Umschalten (Pro Tools macht das selbst).
        else:
            logging.warning("Transport nicht bestätigt gestoppt – State-Wiederherstellung übersprungen.")
            # Pre-Roll trotzdem zurücksetzen (gefahrlos)
            restore_preroll(engine)

        logging.info("Punch-In Durchlauf abgeschlossen.")

    except Exception as e:
        logging.error(f"Fehler: {e}", exc_info=True)
    finally:
        gc.collect()
        with state.running_lock:
            state.running = False
        _set_busy(False)
        logging.info("=== PunchBuddy ENDE ===")

# ─────────────────────────────────────────────────────────────────────────────
# Play (Monitor Auto) Workflow
# ─────────────────────────────────────────────────────────────────────────────

def run_play_custom():
    """Play/Stop-Toggle mit speziellen Mute-States für KH2 und ST Abh.
    Stop-Pfad: delegiert an run_stop() – funktioniert auch während einer Aufnahme.
    Start-Pfad: mutet KH2, entmutet ST Abh und startet Playback."""
    import ptsl.PTSL_pb2 as pt

    engine = _get_engine()
    if engine is None:
        logging.error("PTSL Engine nicht verfügbar – Abbruch (Play Custom).")
        return

    ok_ts, ts_r = _ptsl_call(engine.transport_state, label="PlayCustomTransportState", timeout=6.0)
    if not ok_ts:
        logging.warning("Play Custom: Konnte Transport-State nicht lesen.")
        return

    state_str = str(ts_r)
    logging.info(f"Play Custom: Transport-State: {state_str}")

    # ── STOP-Pfad ────────────────────────────────────────────────────────────
    if state_str in ("TS_TransportRecording", "TS_TransportPlaying",
                     "TS_TransportIsCued", "TS_TransportIsCuedForPreview",
                     "TS_TransportIsStopping"):
        _since = time.time() - _last_transport_start
        if _since < TRANSPORT_START_GRACE:
            logging.info(f"Play Custom: Stop verworfen – Transport läuft erst {_since:.1f}s "
                         f"(Schutzfenster {TRANSPORT_START_GRACE:.0f}s gegen Doppeldruck).")
            return
        logging.info("Play Custom: Transport active – stopping (via run_stop)...")
        run_stop()
        return

    # ── START-Pfad ───────────────────────────────────────────────────────────
    _since_stop = time.time() - _last_stop_request
    if _since_stop < TRANSPORT_STOP_GRACE:
        logging.info(f"Play Custom: Start verworfen – Stop wurde vor {_since_stop:.1f}s ausgelöst "
                     f"(Schutzfenster {TRANSPORT_STOP_GRACE:.0f}s gegen Doppeldruck).")
        return
    with state.running_lock:
        if state.running:
            logging.warning("Play Custom: Script running – start ignored.")
            return
        state.running = True
    _set_busy(True)
    try:
        cfg = state.app_ref.settings if state.app_ref is not None else load_settings()
        ch1 = cfg.get("play_custom_ch1_track", "")
        ch1_mute_start = cfg.get("play_custom_ch1_mute_start", True)
        ch2 = cfg.get("play_custom_ch2_track", "")
        ch2_mute_start = cfg.get("play_custom_ch2_mute_start", False)

        logging.info("Play Custom Start: setting track mute states...")
        if ch1:
            _ptsl_call(engine.set_track_mute_state, [ch1], ch1_mute_start,
                       label="PlayCustomMuteCh1", timeout=5.0)
        if ch2:
            _ptsl_call(engine.set_track_mute_state, [ch2], ch2_mute_start,
                       label="PlayCustomMuteCh2", timeout=5.0)

        state.play_custom_active = True

        time.sleep(0.3)
        logging.info("Play Custom Start: starting playback...")
        _ptsl_call(engine.toggle_play_state, label="PlayCustomToggle", timeout=6.0)
        _mark_transport_start()

    except Exception as e:
        logging.error(f"Error in run_play_custom: {e}", exc_info=True)
        state.play_custom_active = False
    finally:
        with state.running_lock:
            state.running = False
        _set_busy(False)


def run_play():
    """Play/Stop-Toggle.
    Stop-Pfad: delegiert an run_stop() – funktioniert auch während einer Aufnahme.
    Start-Pfad: setzt Input Monitor EIN und startet Playback."""
    import ptsl.PTSL_pb2 as pt

    # Transport-State zuerst lesen – ohne state.running_lock, damit Stop auch während
    # einer laufenden Aufnahme (run_punch_in) ausgelöst werden kann.
    engine = _get_engine()
    if engine is None:
        logging.error("PTSL Engine nicht verfügbar – Abbruch (Play).")
        return

    ok_ts, ts_r = _ptsl_call(engine.transport_state, label="PlayTransportState", timeout=6.0)
    if not ok_ts:
        logging.warning("Play: Konnte Transport-State nicht lesen.")
        return

    state_str = str(ts_r)
    logging.info(f"Play: Transport-State: {state_str}")

    # ── STOP-Pfad ────────────────────────────────────────────────────────────
    if state_str in ("TS_TransportRecording", "TS_TransportPlaying",
                     "TS_TransportIsCued", "TS_TransportIsCuedForPreview",
                     "TS_TransportIsStopping"):
        _since = time.time() - _last_transport_start
        if _since < TRANSPORT_START_GRACE:
            logging.info(f"Play: Stop verworfen – Transport läuft erst {_since:.1f}s "
                         f"(Schutzfenster {TRANSPORT_START_GRACE:.0f}s gegen Doppeldruck).")
            return
        logging.info("Play: Transport aktiv – stoppe (via run_stop)...")
        run_stop()
        return

    # ── START-Pfad ───────────────────────────────────────────────────────────
    _since_stop = time.time() - _last_stop_request
    if _since_stop < TRANSPORT_STOP_GRACE:
        logging.info(f"Play: Start verworfen – Stop wurde vor {_since_stop:.1f}s ausgelöst "
                     f"(Schutzfenster {TRANSPORT_STOP_GRACE:.0f}s gegen Doppeldruck).")
        return
    with state.running_lock:
        if state.running:
            logging.warning("Play: Script läuft bereits – Play-Start ignoriert.")
            return
        state.running = True
    _set_busy(True)
    try:
        settings = load_settings()
        configured_play = settings.get("play_monitor_tracks", [])

        if configured_play:
            logging.info(f"Play: Konfigurierte Play-Spuren: {sorted(configured_play)}")
            tracks_to_mon = list(configured_play)
        else:
            logging.info("Play: Keine Play-Spuren konfiguriert – ermittle selektierte Spuren...")
            tracks_to_mon = []

            # Methode 1: Track-Filter "Selected"
            try:
                sel_filter = pt.TrackListInvertibleFilter(filter=pt.Selected, is_inverted=False)
                ok_sel, selected_tracks = _ptsl_call(
                    lambda: engine.track_list(filters=[sel_filter]),
                    label="TrackListSelected", timeout=5.0
                )
                if ok_sel and selected_tracks:
                    logging.info(f"Play: {len(selected_tracks)} selektierte Spur(en) via Filter.")
                    for t in selected_tracks:
                        if t.type == 1:
                            tracks_to_mon.append(t.name)
                        else:
                            logging.debug(f"Play: '{t.name}' (type={t.type}) kein AudioTrack – übersprungen.")
            except Exception as e:
                logging.warning(f"Play: Filter-Methode fehlgeschlagen ({e}) – Fallback.")

            # Methode 2 (Fallback): alle Tracks iterieren
            if not tracks_to_mon:
                logging.info("Play: Fallback – prüfe is_selected aller Tracks...")
                ok_all, all_tracks = _ptsl_call(engine.track_list, label="TrackListAll", timeout=5.0)
                if ok_all and all_tracks:
                    for t in all_tracks:
                        try:
                            if int(t.track_attributes.is_selected) == 0:
                                continue
                            if t.type != 1:
                                continue
                            tracks_to_mon.append(t.name)
                        except Exception as _e:
                            logging.debug(f"Play: Track '{getattr(t, 'name', '?')}' übersprungen: {_e}")

        logging.info(f"Play: {len(tracks_to_mon)} Monitor-Spuren: {sorted(tracks_to_mon)}")

        if tracks_to_mon:
            mon_ok = False
            for attempt in range(3):
                ok, _ = _ptsl_call(
                    engine.set_track_input_monitor_state,
                    sorted(tracks_to_mon), True,
                    label=f"PlayMonOn#{attempt+1}", timeout=5.0
                )
                if ok:
                    logging.info("Play: Input Monitor EIN OK")
                    mon_ok = True
                    break
                logging.warning(f"Play: Input Monitor EIN FEHLER (Versuch {attempt+1}/3)")
                time.sleep(0.5 * (attempt + 1))
            state.play_monitor_tracks = list(tracks_to_mon) if mon_ok else []
            if not mon_ok:
                logging.warning("Play: Monitor-EIN fehlgeschlagen – keine Spuren gemerkt.")
        else:
            state.play_monitor_tracks = []
            logging.info("Play: Keine Spur selektiert – starte ohne Monitor-Änderung.")

        time.sleep(0.3)
        logging.info("Play: Starte Playback...")
        _ptsl_call(engine.toggle_play_state, label="PlayToggle", timeout=6.0)
        _mark_transport_start()

    except Exception as e:
        logging.error(f"Fehler in run_play: {e}", exc_info=True)
    finally:
        with state.running_lock:
            state.running = False
        _set_busy(False)


def run_stop():
    """Dedizierter Stop: entspricht der Leertaste in Pro Tools.
    Stoppt sowohl Play als auch Recording — UNABHÄNGIG von state.running,
    damit ein laufender Punch-In jederzeit unterbrochen werden kann."""

    # Eigener Lock verhindert Doppel-Stop, blockiert aber keine Aufnahme
    if not _stop_lock.acquire(blocking=False):
        logging.info("Stop: bereits in Ausführung – ignoriert.")
        return
    try:
        engine = _get_engine()
        if engine is None:
            logging.error("PTSL Engine nicht verfügbar – Abbruch (Stop).")
            return

        ok_ts, ts_r = _ptsl_call(engine.transport_state, label="StopTransportState", timeout=6.0)
        if not ok_ts:
            logging.warning("Stop: Konnte Transport-State nicht lesen.")
            return

        state_str = str(ts_r)
        logging.info(f"Stop: Transport-State: {state_str}")

        if state_str in ("TS_TransportStopped", "TS_TransportIsStopping"):
            logging.info("Stop: Transport steht bereits – nichts zu tun.")
            return

        # toggle_play_state = Leertaste: stoppt Play UND Recording
        _mark_stop_request()
        logging.info("Stop: Sende Stop (toggle_play_state)...")
        _ptsl_call(engine.toggle_play_state, label="StopToggle", timeout=6.0)

        # Kurze Pause: PT braucht Zeit für den NEXIS-Write ohne PTSL-Unterbrechung
        time.sleep(1.0)
        # Warten bis PT gestoppt – bei 2 aufeinanderfolgenden Fehlern Abbruch (PT hängt)
        consecutive_errors = 0
        for _ in range(20):
            time.sleep(0.5)
            ok2, ts2 = _ptsl_call(engine.transport_state, label="StopWait", timeout=3.0)
            if ok2:
                consecutive_errors = 0
                if str(ts2) in ("TS_TransportStopped", "TS_TransportIsStopping"):
                    break
            else:
                err_str = str(ts2).lower() if ts2 else ""
                if "closed channel" in err_str or "connection refused" in err_str:
                    logging.warning("Stop: PTSL-Kanal geschlossen – PT nicht mehr erreichbar.")
                    break
                consecutive_errors += 1
                if consecutive_errors >= 2:
                    logging.warning("Stop: 2x PTSL-Fehler – PT möglicherweise hängend, Abbruch.")
                    break

        # Play-Monitor zurücksetzen falls ein /play aktiv war
        if state.play_monitor_tracks:
            time.sleep(0.3)
            logging.info(f"Stop: Input Monitor AUS für {sorted(state.play_monitor_tracks)}...")
            for attempt in range(3):
                ok, _ = _ptsl_call(
                    engine.set_track_input_monitor_state,
                    sorted(state.play_monitor_tracks), False,
                    label=f"StopMonOff#{attempt+1}", timeout=5.0
                )
                if ok:
                    logging.info("Stop: Input Monitor AUS OK")
                    break
                logging.warning(f"Stop: Input Monitor AUS FEHLER (Versuch {attempt+1}/3)")
                time.sleep(0.5 * (attempt + 1))
            state.play_monitor_tracks = []

        # Play-Custom-Mutes zurücksetzen falls ein /play_custom aktiv war
        if state.play_custom_active:
            time.sleep(0.3)
            cfg = state.app_ref.settings if state.app_ref is not None else load_settings()
            ch1 = cfg.get("play_custom_ch1_track", "")
            ch1_mute_stop = cfg.get("play_custom_ch1_mute_stop", False)
            ch2 = cfg.get("play_custom_ch2_track", "")
            ch2_mute_stop = cfg.get("play_custom_ch2_mute_stop", True)
            logging.info(f"Stop: Custom Play war aktiv – Mute-States wiederherstellen "
                         f"({ch1}→{'mute' if ch1_mute_stop else 'unmute'}, "
                         f"{ch2}→{'mute' if ch2_mute_stop else 'unmute'})...")
            ok1, ok2 = True, True
            if ch1:
                ok1, _ = _ptsl_call(engine.set_track_mute_state, [ch1], ch1_mute_stop,
                                    label="StopRestoreCh1", timeout=5.0)
            if ch2:
                ok2, _ = _ptsl_call(engine.set_track_mute_state, [ch2], ch2_mute_stop,
                                    label="StopRestoreCh2", timeout=5.0)
            if ok1 and ok2:
                logging.info("Stop: Mute-States wiederherstellen OK")
            else:
                if not ok1: logging.warning(f"Stop: Mute-Restore '{ch1}' FEHLER (Track ggf. nicht in Session)")
                if not ok2: logging.warning(f"Stop: Mute-Restore '{ch2}' FEHLER (Track ggf. nicht in Session)")
            state.play_custom_active = False

        # Schutzfenster ab dem BESTÄTIGTEN Stopp neu starten: erst jetzt beginnt
        # die Gefahrenzone, in der ein „Nachdruck" als Play-Start durchginge.
        _mark_stop_request()
        logging.info("Stop: abgeschlossen.")

    except Exception as e:
        logging.error(f"Fehler in run_stop: {e}", exc_info=True)
    finally:
        _stop_lock.release()

def run_goto_start():
    """Setzt den Pro Tools Cursor auf den in den Einstellungen definierten Start-Timecode."""
    engine = _get_engine()
    if engine is None:
        logging.error("PTSL Engine nicht verfügbar – Abbruch (GotoStart).")
        return
    tc = load_settings().get("export_start_tc", "10:00:00:00") + ".00"
    logging.info(f">>> GOTO START: {tc} <<<")
    ok, _ = _ptsl_call(
        lambda: engine.set_timeline_selection(in_time=tc, out_time=tc),
        label="GotoStart", timeout=5.0
    )
    if ok:
        logging.info(f"GotoStart: Cursor auf {tc} gesetzt.")
    else:
        logging.warning(f"GotoStart: set_timeline_selection fehlgeschlagen.")

# ─────────────────────────────────────────────────────────────────────────────
# Audio verschieben (Spur-Move via PTSL cut/paste)
# ─────────────────────────────────────────────────────────────────────────────

def run_move_audio(source_tracks, target_tracks):
    """Verschiebt das gesamte Audio-Material der Quell-Spuren auf die Ziel-Spuren
    – an gleicher Zeitposition – via PTSL cut/paste.

    Es wird paarweise pro Spur verschoben (src[i] → tgt[i]):
      1. select_all_clips_on_track(src[i]) → clip-genaue Edit-Selection.
      2. cut().
      3. Zielspur wählen, Cursor auf den ursprünglichen Clip-Start → paste().

    Warum pro Spur und clip-basiert: Eine zeitbereich-basierte Multi-Track-
    Selektion (set_timeline_selection mit min/max) schneidet bei Material, das
    nicht frame-genau beginnt (Subframes), nur unzuverlässig aus. Die clip-
    basierte Einzelspur-Selektion ist robust und vom Start-Timecode unabhängig;
    die Zuordnung ist explizit per Index statt über PTs top→bottom-Mapping.

    Selector-Tool und die Edit-Selection-Links ("Link Timeline/Track and Edit
    Selection") werden über _protools_selection_context gesetzt – nur dann
    operieren cut/paste auf der korrekten Edit-Selection – und danach wieder
    hergestellt."""
    src = [s for s in (source_tracks or []) if s]
    tgt = [t_ for t_ in (target_tracks or []) if t_]
    if not src or not tgt:
        logging.warning("Move-Audio: Quell-/Ziel-Spuren nicht konfiguriert.")
        _show_error("Audio verschieben",
                    "Quell- und Ziel-Spuren sind nicht konfiguriert "
                    "(Einstellungen → Spuren → Audio verschieben).")
        return

    with state.running_lock:
        if state.running:
            logging.warning("Move-Audio: Script läuft bereits – ignoriert.")
            return
        state.running = True
    _set_busy(True)

    try:
        engine = _get_engine()
        if engine is None:
            logging.error("Move-Audio: PTSL Engine nicht verfügbar – Abbruch.")
            return

        logging.info(f"=== MOVE AUDIO: {src} -> {tgt} ===")

        # cut/paste operieren auf der EDIT-Selection. Damit
        # select_tracks_by_name + set_timeline_selection diese über mehrere
        # Spuren hinweg steuern, müssen in Pro Tools "Link Timeline and Edit
        # Selection" + "Link Track and Edit Selection" aktiv und das
        # Selector-Tool gewählt sein. Genau das macht der Export-Context-
        # Manager (und stellt die Originaleinstellungen danach wieder her).
        # Lazy-Import, da export.py seinerseits aus transport.py importiert.
        from punchbuddy.export import _protools_selection_context

        if len(src) != len(tgt):
            logging.warning(f"Move-Audio: {len(src)} Quell- vs. {len(tgt)} Ziel-Spuren – "
                            f"es werden die ersten {min(len(src), len(tgt))} paarweise verschoben.")
        pairs = list(zip(src, tgt))

        with _protools_selection_context(engine):
            time.sleep(0.3)

            # Pro Spur EINZELN verschieben (clip-basiert): select_all_clips_on_track
            # setzt eine clip-genaue Edit-Selection – im Gegensatz zu einer
            # zeitbereich-basierten Selektion funktioniert der cut damit auch bei
            # Material, das nicht frame-genau beginnt (Subframes). Die Zuordnung
            # ist explizit per Index (src[i] -> tgt[i]) statt über PTs
            # top→bottom-Mapping, und jede Spur landet an ihrer eigenen
            # Originalposition – unabhängig vom Start-Timecode.
            moved = 0
            for s_trk, t_trk in pairs:
                # Leer-Erkennung: bei leeren Spuren ändert select_all_clips die
                # Selektion nicht (stale). Daher vorher auf Null-Länge setzen –
                # bleibt sie danach null, ist die Spur leer und wird übersprungen.
                ok_cur, cur = _ptsl_call(engine.get_timeline_selection,
                                         label="MoveCurSel", timeout=5.0)
                reset_tc = cur[0] if (ok_cur and cur and cur[0]) else None
                if reset_tc:
                    _ptsl_call(lambda r=reset_tc: engine.set_timeline_selection(in_time=r, out_time=r),
                               label="MoveResetSel", timeout=5.0)
                    time.sleep(0.2)

                # 1. Quell-Clips clip-basiert selektieren
                ok, _ = _ptsl_call(lambda s=s_trk: engine.select_all_clips_on_track(s),
                                   label=f"MoveSelClips:{s_trk}", timeout=8.0)
                time.sleep(0.3)
                ok2, sel = _ptsl_call(engine.get_timeline_selection,
                                      label=f"MoveGetSel:{s_trk}", timeout=5.0)
                if not (ok and ok2 and sel and sel[0] and sel[1] and sel[0] != sel[1]):
                    logging.info(f"Move-Audio: '{s_trk}' ist leer – übersprungen.")
                    continue
                clip_start = sel[0]

                # 2. Ausschneiden
                ok_cut, _ = _ptsl_call(engine.cut, label=f"MoveCut:{s_trk}", timeout=15.0)
                time.sleep(0.4)
                if not ok_cut:
                    logging.error(f"Move-Audio: Cut '{s_trk}' fehlgeschlagen – übersprungen.")
                    continue

                # 3. Auf Zielspur an gleicher Position einfügen
                _ptsl_call(lambda t=t_trk: engine.select_tracks_by_name([t]),
                           label=f"MoveSelTgt:{t_trk}", timeout=5.0)
                time.sleep(0.3)
                _ptsl_call(lambda c=clip_start: engine.set_timeline_selection(in_time=c, out_time=c),
                           label="MoveCursor", timeout=5.0)
                time.sleep(0.3)
                ok_paste, _ = _ptsl_call(engine.paste, label=f"MovePaste:{t_trk}", timeout=15.0)
                time.sleep(0.4)
                if not ok_paste:
                    logging.error(f"Move-Audio: Paste '{t_trk}' fehlgeschlagen.")
                    _show_error("Audio verschieben",
                                f"Einfügen auf '{t_trk}' fehlgeschlagen. Das Material liegt "
                                f"in der Zwischenablage und kann mit Cmd+V eingefügt werden.")
                    continue

                moved += 1
                logging.info(f"Move-Audio: '{s_trk}' -> '{t_trk}' @ {clip_start}")

            if moved == 0:
                logging.warning("Move-Audio: nichts verschoben (keine Quell-Spur mit Material).")
                _show_error("Audio verschieben", "Auf den Quell-Spuren liegt kein Material.")
            else:
                logging.info(f"=== MOVE AUDIO abgeschlossen: {moved} Spur(en) verschoben ===")

    except Exception as e:
        logging.error(f"Move-Audio Fehler: {e}", exc_info=True)
    finally:
        with state.running_lock:
            state.running = False
        _set_busy(False)

# ─────────────────────────────────────────────────────────────────────────────
# Menüleisten-Status (Idle / Busy)
# ─────────────────────────────────────────────────────────────────────────────

def _set_busy(busy: bool):
    """Setzt den Menüleisten-Indikator auf rot (busy) oder normal (idle).

    Der rumps-Title-Setter ruft NSStatusItem.setTitle_ (AppKit) auf – das
    darf nur auf dem Main-Thread passieren. Aufrufer laufen aber meist in
    Worker-Threads (Punch-In/Export/Import), daher wird hier immer auf den
    Main-Thread dispatcht."""
    if state.app_ref is None:
        return

    def _do():
        try:
            state.app_ref.title = state.ICON_BUSY if busy else state.ICON_IDLE
        except Exception:
            pass
    _dispatch_main(_do)

# ─────────────────────────────────────────────────────────────────────────────
# Export-Workflow
# ─────────────────────────────────────────────────────────────────────────────
_export_lock = threading.Lock()
_EXTEND_COUNT = 7  # Anzahl Spuren unter V1 die ausgedehnt werden

def _send_shift_oe(count=1):
    """Sendet Shift+Ö an Pro Tools um die Edit-Selection nach unten auszudehnen."""
    try:
        import Quartz
        pid = _pt_pid()
        if not pid:
            logging.warning("Shift+Ö: PT PID nicht gefunden")
            return False
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        for _ in range(count):
            ev_dn = Quartz.CGEventCreateKeyboardEvent(src, _VK_OE, True)
            ev_up = Quartz.CGEventCreateKeyboardEvent(src, _VK_OE, False)
            Quartz.CGEventSetFlags(ev_dn, Quartz.kCGEventFlagMaskShift)
            Quartz.CGEventSetFlags(ev_up, Quartz.kCGEventFlagMaskShift)
            Quartz.CGEventPostToPid(pid, ev_dn)
            Quartz.CGEventPostToPid(pid, ev_up)
            time.sleep(0.05)
        return True
    except Exception as e:
        logging.error(f"Shift+Ö senden: {e}")
        return False
