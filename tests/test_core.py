"""Hardware-freie Unit-Tests für die Kern-Helfer von PunchBuddy.

Setzt voraus, dass `import auto_punch_in` nebenwirkungsfrei ist (keine
Settings-Migration / kein Sprach-Laden beim Import – das passiert erst in
init_runtime()).
"""
import json
import os
import socket
import tempfile
import threading
import time
import types
import grpc
import pytest

import auto_punch_in as a
import punchbuddy.config as cfg
import punchbuddy.keys as keys
import punchbuddy.engine as engine


# ── _deep_merge ───────────────────────────────────────────────────────────
def test_deep_merge_fills_missing_nested_keys():
    default = {"a": 1, "nested": {"x": 1, "y": 2}}
    override = {"nested": {"y": 99}}
    out = a._deep_merge(default, override)
    assert out == {"a": 1, "nested": {"x": 1, "y": 99}}


def test_deep_merge_override_wins_for_lists_and_scalars():
    default = {"lst": [1, 2, 3], "s": "def"}
    override = {"lst": [9], "s": "usr"}
    assert a._deep_merge(default, override) == {"lst": [9], "s": "usr"}


def test_deep_merge_does_not_mutate_default():
    default = {"nested": {"x": 1}}
    a._deep_merge(default, {"nested": {"y": 2}})
    assert default == {"nested": {"x": 1}}


# ── load_settings (Deep-Merge + Preset-Backfill) ──────────────────────────
def test_load_settings_backfills_new_default_keys(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    # Alte Datei ohne den (neueren) Key webtrigger_token
    settings_file.write_text(json.dumps({"http_port": 1234}))
    monkeypatch.setattr(cfg, "SETTINGS_PATH", str(settings_file))

    s = a.load_settings()
    assert s["http_port"] == 1234                      # Nutzerwert bleibt
    assert "webtrigger_token" in s                     # neuer Default ergänzt
    assert s["webtrigger_token"] == a.DEFAULT_SETTINGS["webtrigger_token"]


def test_load_settings_backfills_preset_entry_keys(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    # Preset-Eintrag ohne 'export' (neu hinzugekommener Key)
    settings_file.write_text(json.dumps({"track_presets": [{"name": "P1", "rec_a": ["ST"]}]}))
    monkeypatch.setattr(cfg, "SETTINGS_PATH", str(settings_file))

    s = a.load_settings()
    p = s["track_presets"][0]
    assert p["name"] == "P1"
    assert p["rec_a"] == ["ST"]
    assert p["export"] == []        # aus _PRESET_TEMPLATE aufgefüllt
    assert "mon_a" in p


# ── webtrigger_token_ok ───────────────────────────────────────────────────
def test_token_empty_means_open():
    assert a.webtrigger_token_ok("", "irgendwas") is True
    assert a.webtrigger_token_ok("", "") is True


def test_token_required_and_matching():
    assert a.webtrigger_token_ok("secret", "secret") is True


@pytest.mark.parametrize("supplied", ["", "wrong", "secre", "Secret"])
def test_token_mismatch_rejected(supplied):
    assert a.webtrigger_token_ok("secret", supplied) is False


# ── _pt_pid Cache-Validierung ─────────────────────────────────────────────
def test_pt_pid_returns_live_cached_pid(monkeypatch):
    import os
    monkeypatch.setattr(keys, "_cached_pid", os.getpid())
    assert a._pt_pid() == os.getpid()


def test_pt_pid_invalidates_dead_cached_pid(monkeypatch):
    # Tote PID im Cache + pgrep liefert nichts → None und Cache geleert
    monkeypatch.setattr(keys, "_cached_pid", 2_000_000_000)

    class _Empty:
        stdout = ""
    monkeypatch.setattr(keys.subprocess, "run", lambda *args, **kw: _Empty())
    assert a._pt_pid() is None
    assert keys._cached_pid is None


# ── _app_pid Cache-Validierung ────────────────────────────────────────────
def test_app_pid_invalidates_dead_entry(monkeypatch):
    keys._app_pid_cache["GhostApp"] = 2_000_000_000

    class _Empty:
        stdout = ""
    monkeypatch.setattr(keys.subprocess, "run", lambda *args, **kw: _Empty())
    # AppKit-Pfad findet nichts (Name existiert nicht) → pgrep leer → None
    assert a._app_pid("GhostApp") is None
    assert "GhostApp" not in keys._app_pid_cache


# ── gRPC-Deadline-Wrapper ─────────────────────────────────────────────────
class _FakeRaw:
    def __init__(self):
        self.captured = {}

    def SendGrpcRequest(self, request, *args, **kwargs):
        self.captured = dict(kwargs)
        return "resp"


class _FakeEngine:
    def __init__(self):
        self.client = type("C", (), {})()
        self.client.raw_client = _FakeRaw()


def test_install_grpc_deadline_injects_timeout():
    eng = _FakeEngine()
    a._install_grpc_deadline(eng)
    a._grpc_deadline_tls.value = 7.5
    try:
        eng.client.raw_client.SendGrpcRequest("req")
    finally:
        a._grpc_deadline_tls.value = a._GRPC_CALL_DEADLINE
    assert eng.client.raw_client.captured.get("timeout") == 7.5


def test_install_grpc_deadline_idempotent():
    eng = _FakeEngine()
    a._install_grpc_deadline(eng)
    wrapped_once = eng.client.raw_client.SendGrpcRequest
    a._install_grpc_deadline(eng)
    assert eng.client.raw_client.SendGrpcRequest is wrapped_once


# ── _ptsl_call Verhalten ──────────────────────────────────────────────────
def test_ptsl_call_happy_path_sets_deadline():
    seen = {}

    def fn(x):
        seen["deadline"] = a._current_grpc_deadline()
        return x * 2

    ok, res = a._ptsl_call(fn, 21, label="T", timeout=12.0)
    assert ok is True and res == 42
    assert seen["deadline"] == 12.0
    # Nach dem Call wieder auf Default zurückgesetzt
    assert a._current_grpc_deadline() == a._GRPC_CALL_DEADLINE


def test_ptsl_call_command_error_keeps_engine(monkeypatch):
    reset_called = {"n": 0}
    monkeypatch.setattr(engine, "_reset_engine", lambda stale=None: reset_called.__setitem__("n", reset_called["n"] + 1))

    def fn():
        raise ValueError("fachlicher Fehler")

    ok, res = a._ptsl_call(fn, label="T")
    assert ok is False
    assert isinstance(res, ValueError)
    assert reset_called["n"] == 0          # KEIN Reconnect bei Nicht-RPC-Fehler


def test_ptsl_call_rpc_deadline_resets_engine(monkeypatch):
    reset_called = {"n": 0}
    monkeypatch.setattr(engine, "_reset_engine", lambda stale=None: reset_called.__setitem__("n", reset_called["n"] + 1))

    class _Deadline(grpc.RpcError):
        def code(self):
            return grpc.StatusCode.DEADLINE_EXCEEDED

    def fn():
        raise _Deadline()

    ok, res = a._ptsl_call(fn, label="T", timeout=5.0)
    assert ok is False and res is None     # Timeout → (False, None)
    assert reset_called["n"] == 1          # Verbindung verworfen


# ── Zentrale Befehls-API (command_dispatch) ───────────────────────────────
def _fake_app(**settings):
    """Baut ein minimales App-Stand-in mit den echten Dispatcher-Methoden
    (ohne AppKit/rumps zu starten). Stubbt die _trigger_*-Worker."""
    App = a.PunchBuddyApp

    class _Fake:
        pass

    f = _Fake()
    f._last_trigger = {}
    f._trigger_lock = threading.Lock()
    f.settings = {"http_port": 8899}
    f.settings.update(settings)
    f.vocaster = None
    f._cmd_table_cache = None
    f._http_port = 8899
    f.calls = []
    for name in ["_trigger", "_trigger_b", "_trigger_play", "_trigger_play_custom",
                 "_trigger_start", "_trigger_move_audio", "_trigger_import",
                 "_trigger_export_wav", "_trigger_export_aaf",
                 "_trigger_export_aaf_reference", "_trigger_export_interplay"]:
        setattr(f, name, (lambda n=name: f.calls.append(n)))
    f.load_preset_by_index = lambda idx: (f.calls.append(("preset", idx)) or 0 <= idx < 8)
    for m in ["_command_table", "command_dispatch", "_cmd_preset", "_cmd_vocaster",
              "_cmd_status", "_cmd_list", "_debounce_ok", "_dispatch_socket_line",
              "_start_unix_socket", "_close_unix_socket"]:
        setattr(f, m, types.MethodType(getattr(App, m), f))
    return f


def test_dispatch_simple_command_calls_trigger():
    f = _fake_app()
    ok, msg = f.command_dispatch("play")
    assert ok is True and "queued" in msg
    assert f.calls == ["_trigger_play"]


@pytest.mark.parametrize("alias,trigger", [
    ("trigger", "_trigger"),
    ("trigger2", "_trigger_b"),
    ("a", "_trigger"),
    ("b", "_trigger_b"),
    ("start", "_trigger_start"),
    ("move", "_trigger_move_audio"),
    ("export_aaf_embedded", "_trigger_export_aaf"),
])
def test_dispatch_aliases_resolve(alias, trigger):
    f = _fake_app()
    ok, _ = f.command_dispatch(alias)
    assert ok is True
    assert f.calls == [trigger]


def test_dispatch_is_case_insensitive():
    f = _fake_app()
    assert f.command_dispatch("PLAY")[0] is True
    assert f.calls == ["_trigger_play"]


def test_dispatch_unknown_command():
    f = _fake_app()
    ok, msg = f.command_dispatch("bogus")
    assert ok is False and "unknown command" in msg
    assert f.calls == []


def test_dispatch_preset_valid_and_invalid():
    f = _fake_app()
    ok, msg = f.command_dispatch("preset", ["3"])
    assert ok is True and "loaded" in msg
    assert ("preset", 2) in f.calls          # 1-basiert → idx 2

    ok2, msg2 = f.command_dispatch("preset", ["99"])
    assert ok2 is False and "invalid preset index" in msg2


def test_dispatch_preset_non_numeric():
    f = _fake_app()
    ok, msg = f.command_dispatch("preset", ["xx"])
    assert ok is False and "invalid preset number" in msg


def test_dispatch_preset_without_arg():
    f = _fake_app()
    ok, msg = f.command_dispatch("preset")
    assert ok is False and "needs a number" in msg


def test_dispatch_vocaster_without_device():
    f = _fake_app()
    ok, msg = f.command_dispatch("vocaster_phantom_on")
    assert ok is False and "no vocaster" in msg


def test_dispatch_list_and_ping():
    f = _fake_app()
    ok, msg = f.command_dispatch("list")
    assert ok is True
    for must in ["play", "record_a", "export_wav", "preset", "status"]:
        assert must in msg
    assert f.command_dispatch("ping") == (True, "pong")


def test_socket_line_strips_token_arg():
    f = _fake_app()
    ok, _ = f._dispatch_socket_line("play token=secret")
    assert ok is True
    assert f.calls == ["_trigger_play"]


def test_socket_line_preset_with_arg():
    f = _fake_app()
    ok, msg = f._dispatch_socket_line("preset 2")
    assert ok is True and "loaded" in msg
    assert ("preset", 1) in f.calls


def test_socket_line_empty():
    f = _fake_app()
    assert f._dispatch_socket_line("")[0] is False


def test_preset_debounce_in_dispatcher():
    """Der Preset-Debounce sitzt im Dispatcher (_cmd_preset) und gilt damit
    transport-unabhängig: der zweite identische Aufruf wird verworfen."""
    f = _fake_app()
    ok1, _ = f.command_dispatch("preset", ["3"])
    ok2, msg2 = f.command_dispatch("preset", ["3"])   # sofort danach → entprellt
    assert ok1 is True
    assert ok2 is True and "debounced" in msg2
    assert f.calls.count(("preset", 2)) == 1          # nur EIN echtes Laden


# ── Unix-Socket-Server (Integration, lokal) ───────────────────────────────
# Kurzer Pfad nötig: macOS AF_UNIX erlaubt max. 104 Zeichen (pytest tmp_path
# ist zu lang). Der Produktiv-Default /tmp/punchbuddy.sock ist kurz genug.
def test_unix_socket_server_roundtrip():
    sock_path = f"/tmp/pb_unittest_{os.getpid()}.sock"
    f = _fake_app(unix_socket_enabled=True, unix_socket_path=sock_path)
    f._start_unix_socket()
    try:
        time.sleep(0.2)
        assert os.path.exists(sock_path)
        # Datei-Rechte 0600 (nur Besitzer)
        import stat
        assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600

        def send(line):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(sock_path)
            c.sendall((line + "\n").encode())
            resp = c.recv(1024).decode().strip()
            c.close()
            return resp

        assert send("play") == "OK play queued"
        assert send("preset 4").startswith("OK")
        assert send("bogus").startswith("ERR")
        # Überlange Zeile ohne Zeilenende → sauber abgelehnt (kein stilles Abschneiden)
        assert send("x" * 5000) == "ERR line too long"
        assert "_trigger_play" in f.calls
    finally:
        f._close_unix_socket()
        time.sleep(0.1)
    assert not os.path.exists(sock_path)      # Socket sauber entfernt


def test_unix_socket_disabled_does_not_bind():
    sock_path = f"/tmp/pb_unittest_off_{os.getpid()}.sock"
    f = _fake_app(unix_socket_enabled=False, unix_socket_path=sock_path)
    f._start_unix_socket()
    time.sleep(0.1)
    assert not os.path.exists(sock_path)
