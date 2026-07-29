"""PTSL-Engine-Verwaltung, Aufruf-Serialisierung mit gRPC-Deadline,
Track-Cache und Pre-Roll-Steuerung.

Importiert nur nach „unten" (keys); Transport/Export/UI importieren von hier.
"""
import time
import logging
import threading
import subprocess

import ptsl

from punchbuddy.keys import _send_key, _VK_K

def _get_preroll_status(engine):
    """Liest den Pre-Roll-Status direkt aus Pro Tools via PTSL.
    Gibt (ok, enabled) zurück. ok=False bei Kommunikationsfehler."""
    try:
        import ptsl.ops as ops
        import ptsl.PTSL_pb2 as pt
        op = ops.CId_GetTimelineSelection(
            location_type=pt.TLType_TimeCode)
        ok, _ = _ptsl_call(
            lambda: engine.client.run(op) or True,
            label="GetPreRoll", timeout=8.0
        )
        if ok:
            return True, bool(op.response.pre_roll_enabled)
        return False, None
    except Exception as e:
        logging.warning(f"[GetPreRoll] Exception: {e}")
        return False, None

def _set_preroll_state(engine, enabled: bool):
    """Setzt Pre-Roll ein/aus via PTSL.
    Liest zuerst die aktuelle Timeline-Selection, damit in_time/out_time
    beim Setzen erhalten bleiben (sonst springt der Cursor zum Anfang).
    Gibt True bei Erfolg zurück."""
    try:
        import ptsl.ops as ops
        import ptsl.PTSL_pb2 as pt
        tb_val = pt.TB_True if enabled else pt.TB_False

        # 1. Aktuelle Timeline-Selection lesen (in_time benötigt!)
        get_op = ops.CId_GetTimelineSelection(location_type=pt.TLType_TimeCode)
        ok_get, _ = _ptsl_call(
            lambda: engine.client.run(get_op) or True,
            label="GetTimeline", timeout=8.0
        )
        if ok_get and get_op.response.in_time:
            # in_time zurückgeben damit Cursor-Position erhalten bleibt
            ok, _ = _ptsl_call(
                lambda: engine.set_timeline_selection(
                    in_time=get_op.response.in_time,
                    pre_roll_enabled=tb_val
                ),
                label=f"SetPreRoll({'EIN' if enabled else 'AUS'})",
                timeout=8.0
            )
        else:
            # Fallback: ohne in_time (nur pre_roll_enabled)
            set_op = ops.CId_SetTimelineSelection(pre_roll_enabled=tb_val)
            ok, _ = _ptsl_call(
                lambda: engine.client.run(set_op),
                label=f"SetPreRoll({'EIN' if enabled else 'AUS'})",
                timeout=8.0
            )
        return ok
    except Exception as e:
        logging.warning(f"[SetPreRoll] Exception: {e}")
        return False

def ensure_preroll_on(engine=None):
    """Stellt sicher, dass Pre-Roll VOR der Aufnahme AN ist (PTSL, 3 Versuche).
    Optimiert für Geschwindigkeit – keine Verifikation beim ersten Versuch.
    WICHTIG: Schließt NIE die Engine (shared Singleton mit run_punch_in)."""
    _own_engine = engine is None
    if _own_engine:
        engine = _get_engine()
    if engine is None:
        logging.warning("Pre-Roll EIN: Engine nicht verfügbar – Fallback cmd+k")
        _send_key(_VK_K, cmd=True)
        time.sleep(0.15)
        return

    for attempt in range(3):
        ok, is_on = _get_preroll_status(engine)
        if ok and is_on:
            logging.info("Pre-Roll ist bereits AN – keine Änderung.")
            return

        label = "AUS" if (ok and not is_on) else "unbekannt"
        logging.info(f"Pre-Roll ist {label} – schalte ein (PTSL, Versuch {attempt+1}/3)...")

        success = _set_preroll_state(engine, True)
        if success:
            logging.info("Pre-Roll EIN gesetzt (PTSL).")
            return

        logging.warning(f"Pre-Roll EIN FEHLER (Versuch {attempt+1}/3)")
        time.sleep(0.3)

    logging.error("⚠️  Pre-Roll EIN FEHLGESCHLAGEN nach 3 PTSL-Versuchen – Fallback cmd+k")
    _send_key(_VK_K, cmd=True)
    time.sleep(0.15)

def restore_preroll(engine=None):
    """Stellt sicher, dass Pre-Roll NACH der Aufnahme AUS ist (PTSL, 3 Versuche).
    WICHTIG: Schließt NIE die Engine (shared Singleton mit run_punch_in)."""
    _own_engine = engine is None
    if _own_engine:
        engine = _get_engine()
    if engine is None:
        logging.warning("Pre-Roll AUS: Engine nicht verfügbar – Fallback cmd+k")
        _send_key(_VK_K, cmd=True)
        time.sleep(0.15)
        return

    for attempt in range(3):
        ok, is_on = _get_preroll_status(engine)
        if ok and not is_on:
            logging.info("Pre-Roll ist bereits AUS – keine Änderung.")
            return

        label = "AN" if (ok and is_on) else "unbekannt"
        logging.info(f"Pre-Roll ist {label} – schalte aus (PTSL, Versuch {attempt+1}/3)...")

        success = _set_preroll_state(engine, False)
        if success:
            logging.info("Pre-Roll AUS gesetzt (PTSL).")
            return

        logging.warning(f"Pre-Roll AUS FEHLER (Versuch {attempt+1}/3)")
        time.sleep(0.3)

    logging.error("⚠️  Pre-Roll AUS FEHLGESCHLAGEN nach 3 PTSL-Versuchen – Fallback cmd+k")
    _send_key(_VK_K, cmd=True)
    time.sleep(0.15)



# ─────────────────────────────────────────────────────────────────────────────
# PTSL Helper
# ─────────────────────────────────────────────────────────────────────────────

# ── Singleton Engine (verhindert Verbindungslecks) ───────────────────────
_engine_instance = None
_engine_lock = threading.Lock()

# Harte gRPC-Deadline pro PTSL-Befehl. py-ptsl reicht selbst kein timeout an den
# gRPC-Stub durch, wodurch ein hängender Call (z. B. NEXIS-Cold-Start) ewig
# blockieren konnte. _install_grpc_deadline() injiziert deshalb eine echte
# Deadline; _ptsl_call setzt den konkreten Wert pro Aufruf via Thread-Local.
_GRPC_CALL_DEADLINE = 15.0
_grpc_deadline_tls = threading.local()


def _current_grpc_deadline() -> float:
    return getattr(_grpc_deadline_tls, "value", _GRPC_CALL_DEADLINE)


def _install_grpc_deadline(engine):
    """Hüllt SendGrpcRequest des gRPC-Stubs so, dass jeder Call eine echte
    Deadline erhält (DEADLINE_EXCEEDED statt unbegrenztem Hängen)."""
    try:
        raw = engine.client.raw_client
        orig = raw.SendGrpcRequest
        if getattr(orig, "_pb_deadline_wrapped", False):
            return

        def _with_deadline(request, *args, **kwargs):
            kwargs.setdefault("timeout", _current_grpc_deadline())
            return orig(request, *args, **kwargs)

        _with_deadline._pb_deadline_wrapped = True
        raw.SendGrpcRequest = _with_deadline
    except Exception as e:
        logging.warning(f"gRPC-Deadline-Wrapper nicht installiert: {e}")


def _get_engine():
    """Wiederverwendbare PTSL-Engine (Singleton). Verbindet bei Bedarf neu.

    Kein proaktiver Health-Check mehr: Da jeder Call eine harte gRPC-Deadline
    hat (_install_grpc_deadline), werden tote/blockierte Verbindungen reaktiv
    erkannt – ein fehlgeschlagener _ptsl_call verwirft die Instanz via
    _reset_engine(), der nächste _get_engine() verbindet neu. Der Keep-Alive
    hält die Verbindung zusätzlich warm.
    """
    global _engine_instance
    with _engine_lock:
        if _engine_instance is not None:
            return _engine_instance
        try:
            eng = ptsl.Engine(company_name="PunchBuddy", application_name="PunchBuddy")
            _install_grpc_deadline(eng)
            _engine_instance = eng
            logging.info("PTSL verbunden.")
            return eng
        except Exception as e:
            logging.error(f"PTSL Verbindung fehlgeschlagen: {e}")
            return None


def current_engine():
    """Gibt die aktuelle Engine-Instanz zurück ODER None – ohne zu verbinden.
    Für den Keep-Alive, der nur eine bereits offene Verbindung pingen will."""
    return _engine_instance


def cached_track_count() -> int:
    """Anzahl der aktuell gecachten Track-Namen (0 wenn leer)."""
    with _track_cache_lock:
        return len(_cached_track_names) if _cached_track_names else 0


def _safe_close(eng):
    try:
        eng.close()
    except Exception:
        pass


def _reset_engine(stale=None):
    """Verwirft die aktuelle Engine-Instanz → nächster _get_engine() verbindet
    neu. Wird bei Verbindungsfehlern (gRPC-Deadline/RpcError) aufgerufen.
    Das close() läuft im Hintergrund, damit ein toter Channel den Aufrufer
    nicht blockiert.

    Ist `stale` gesetzt, wird NUR verworfen, wenn es noch die aktuelle Instanz
    ist. Das verhindert, dass ein Worker mit einer gecachten (alten) Engine-
    Referenz eine bereits ersetzte FREMDE Instanz wegschliesst – die häufigste
    Ursache für verwaiste/zombie (CLOSE_WAIT) Verbindungen unter schneller
    Befehlsfolge."""
    global _engine_instance
    with _engine_lock:
        if stale is not None and stale is not _engine_instance:
            return  # eine andere Instanz ist schon aktiv – nicht anfassen
        dead = _engine_instance
        _engine_instance = None
    if dead is not None:
        threading.Thread(target=_safe_close, args=(dead,), daemon=True,
                         name="EngineClose").start()


def _reset_if_rpc_error(e) -> bool:
    """Verwirft die Engine, wenn `e` ein gRPC-Verbindungsfehler (toter Channel)
    ist. Für ROHE engine.*-Aufrufe, die NICHT über _ptsl_call laufen und daher
    sonst keinen Reset auslösen würden → verhindert tote/zombie-Verbindungen.
    Gibt True zurück, wenn ein Reset ausgelöst wurde."""
    try:
        import grpc
        if isinstance(e, grpc.RpcError):
            logging.warning("Roher gRPC-Fehler ausserhalb _ptsl_call – "
                            "Engine wird verworfen (Anti-Zombie).")
            _reset_engine()
            return True
    except Exception:
        pass
    return False


# Rückwärtskompatibler Alias: frühere Aufrufer riefen _invalidate_engine_health.
_invalidate_engine_health = _reset_engine


def _close_engine():
    """Schliesst die Singleton-Engine explizit (Session-Wechsel/Import/Quit)."""
    global _engine_instance
    with _engine_lock:
        dead = _engine_instance
        _engine_instance = None
    if dead is not None:
        _safe_close(dead)
        logging.info("PTSL Engine geschlossen.")
    _invalidate_session_tracks()

_ptsl_lock = threading.Lock()

# ── Session-Track-Cache ───────────────────────────────────────────────────
# Track-Namen ändern sich nicht während einer Session – einmalig lesen und
# cachen. Invalidiert wenn die Engine geschlossen wird (Session-Wechsel,
# Import, Reconnect). Run_play() liest Track-Listen weiterhin live,
# da dort die Selection in Echtzeit benötigt wird.
_cached_track_names: list | None = None
_track_cache_lock = threading.Lock()

def _invalidate_session_tracks():
    global _cached_track_names
    with _track_cache_lock:
        _cached_track_names = None

def refresh_session_tracks(engine) -> bool:
    """Liest Track-Namen frisch aus PT und befüllt den Cache.
    Gibt True zurück bei Erfolg."""
    global _cached_track_names
    ok, tracks = _ptsl_call(engine.track_list, label="TrackListCache", timeout=10.0)
    if ok and tracks is not None:
        names = [t.name for t in tracks]
        with _track_cache_lock:
            _cached_track_names = names
        logging.info(f"Track-Cache befüllt: {len(names)} Spuren.")
        return True
    logging.warning("Track-Cache: track_list() fehlgeschlagen – Cache bleibt leer.")
    return False

def _get_cached_track_names(engine) -> list | None:
    """Gibt gecachte Track-Namen zurück. Befüllt den Cache wenn leer."""
    with _track_cache_lock:
        if _cached_track_names is not None:
            return list(_cached_track_names)
    # Cache leer → einmalig laden
    if refresh_session_tracks(engine):
        with _track_cache_lock:
            return list(_cached_track_names) if _cached_track_names is not None else None
    return None

def _ptsl_call(fn, *args, label: str = "", timeout: float = 15.0):
    """Führt einen PTSL-Aufruf serialisiert (über _ptsl_lock) und mit harter
    gRPC-Deadline aus. Gibt (ok, result) zurück.

    `timeout` ist jetzt die echte gRPC-Deadline des Calls: ein hängender Befehl
    wird vom gRPC-Stack mit DEADLINE_EXCEEDED abgebrochen, statt – wie früher –
    einen verwaisten Thread und gehaltenen Lock zu hinterlassen.

    Nur echte Verbindungsfehler (grpc.RpcError) verwerfen die Engine; ein
    fachlicher CommandError lässt die Verbindung bestehen.
    """
    _t_wait = time.time()
    if not _ptsl_lock.acquire(timeout=6.0):  # 6s – NEXIS kann vorherigen Befehl verzögern
        logging.warning(f"[{label}] PTSL-Lock nicht erhalten (6s)")
        return False, None
    _lock_wait = time.time() - _t_wait

    _grpc_deadline_tls.value = timeout
    _t_call = time.time()
    try:
        _result = fn(*args)
        # Instrumentierung: Pro Tools beantwortet PTSL-Calls bimodal (~10ms ODER
        # 300–1400ms, gemessen 2026-07-21 – auch auf leerer Session). Langsame
        # Calls + Lock-Stau hier sichtbar machen, damit Diagnosen die Wartezeit
        # pro Befehl schwarz auf weiß zeigen, statt sie PunchBuddy zuzuschreiben.
        _dur = time.time() - _t_call
        if _dur > 0.3 or _lock_wait > 0.25:
            _extra = f", Lock-Wartezeit {_lock_wait*1000:.0f}ms" if _lock_wait > 0.25 else ""
            logging.info(f"[{label}] PTSL langsam: Call {_dur*1000:.0f}ms{_extra} (Pro-Tools-seitig)")
        return True, _result
    except Exception as e:
        is_rpc = False
        is_timeout = False
        try:
            import grpc
            if isinstance(e, grpc.RpcError):
                is_rpc = True
                is_timeout = (e.code() == grpc.StatusCode.DEADLINE_EXCEEDED)
        except Exception:
            pass
        if is_timeout:
            logging.warning(f"[{label}] gRPC-Deadline ({timeout}s) überschritten")
        else:
            logging.error(f"[{label}] Fehler: {e}")
        if is_rpc:
            # Nur DIESE Instanz verwerfen (über fn.__self__, falls gebundene
            # Methode), damit kein paralleler Worker eine bereits ersetzte
            # Engine wegschliesst. Bei Lambdas (__self__ fehlt) → unbedingt.
            _reset_engine(stale=getattr(fn, "__self__", None))
        return False, (None if is_timeout else e)
    finally:
        _grpc_deadline_tls.value = _GRPC_CALL_DEADLINE
        _ptsl_lock.release()

def _show_error(title: str, msg: str):
    """Zeigt Fehler-Dialog (non-blocking)."""
    try:
        subprocess.Popen(["osascript", "-e",
            f'display alert "{title}" message "{msg}" as critical'])
    except Exception:
        pass
