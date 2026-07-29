"""Einstellungen: Defaults, Laden/Speichern (Deep-Merge) und Migration."""
import os
import json
import copy
import shutil
import logging

from punchbuddy.log import _LOG_DIR

SETTINGS_PATH = os.path.join(_LOG_DIR, "settings.json")
DEFAULT_SETTINGS = {
    "tracks":          ["ST", "A  IT", "OGA", "Mus", "Spr", "Spr 2"],
    "monitor_tracks":  ["ST", "A  IT", "OGA", "Mus", "Spr"],
    "tracks_b":        [],
    "monitor_tracks_b": [],
    "play_monitor_tracks": [],
    "export_tracks":   ["ST", "A  IT", "OGA", "Mus", "Spr"],
    "video_track":     "Video 1",
    "export_start_tc":     "10:00:00:00",
    "loudness_enabled":    True,
    "loudness_tracks":     ["ST"],
    "target_lufs":         -23.0,
    "max_truepeak":        -3.0,
    "extend_count":        7,
    "wav_export_enabled":  False,
    "aaf_export_enabled":  False,
    # Custom Export-Pfade (leer = Standard <session>/export)
    "wav_export_path":           "",
    "aaf_embedded_export_path":  "",
    "aaf_reference_export_path": "",
    # AAF-Reference-Vorgaben (fix laut Workflow)
    "aaf_reference_bit_depth":   24,
    "aaf_reference_handle_ms":   1000,
    "interplay_enabled":   False,
    "interplay_workspace": "001-aktuelles [fad-nexis]",
    "interplay_workspace_steps": 17,
    "export_error_keywords":   "error,fail,fehler,unsuccessful,could not,unable,problem,warning,aborted,abgebrochen",
    "export_success_keywords": "success,complete,finished,done,exported,erfolgreich,abgeschlossen,fertig",
    "interplay_rename_enabled": False,
    "interplay_rename_trim_start": 0,
    "interplay_rename_trim_end": 0,
    "interplay_rename_prefix": "",
    "interplay_rename_suffix": "",
    "track_presets": [
        {"name": f"Preset {i+1}", "rec_a": [], "mon_a": [], "rec_b": [], "mon_b": [], "export": []}
        for i in range(8)
    ],
    "import_close_session": True,
    "http_port": 8899,
    "http_bind_host": "127.0.0.1",
    "webtrigger_token": "",
    # ── Unix-Domain-Socket-Steuerung (netzwerkfrei) ───────────────────────
    # Lokaler IPC-Socket, der dieselbe Befehls-API wie der HTTP-Webtrigger
    # bedient – komplett ohne IP-Stack (kein TCP/Loopback), daher von
    # Netzwerk-Filtern (z. B. Microsoft Defender) prinzipiell nicht erfassbar.
    # Datei-Rechte 0600 = nur der angemeldete Benutzer darf verbinden.
    "unix_socket_enabled": True,
    "unix_socket_path": "/tmp/punchbuddy.sock",
    "language": "de",
    "play_custom_ch1_track": "KH2",
    "play_custom_ch1_mute_start": True,
    "play_custom_ch1_mute_stop": False,
    "play_custom_ch2_track": "ST Abh",
    "play_custom_ch2_mute_start": False,
    "play_custom_ch2_mute_stop": True,
    # ── Audio verschieben (Spur-Move) ─────────────────────────────────────
    # Verschiebt das gesamte Material der Quell-Spuren auf die Ziel-Spuren
    # (gleiche Zeitposition) via PTSL cut/paste. Je 2 Spuren erwartet.
    "move_audio_source_tracks": [],
    "move_audio_target_tracks": [],
    # ── Vocaster ──────────────────────────────────────────────────────────
    # 48V Phantomspeisung beim Start von PunchBuddy automatisch einschalten
    # (nur wirksam wenn ein Vocaster One/Two angeschlossen ist).
    "vocaster_phantom_on_start": False,
    # Gespeichertes Audio-Routing (MUX) beim Start automatisch ans Gerät
    # zurückschreiben. Macht die Vocaster Hub App auch nach Power-Cycles
    # überflüssig. Routing wird per Vocaster-Tab → "Aktuelles Routing
    # speichern" aufgezeichnet.
    "vocaster_apply_routing_on_start": False,
}


# ── Migration: ~/.autopunchin → ~/.punchbuddy ─────────────────────────────
_OLD_SETTINGS_DIR = os.path.expanduser("~/.autopunchin")
_NEW_SETTINGS_DIR = os.path.dirname(SETTINGS_PATH)

def _migrate_settings():
    """Migriert alte Einstellungen von ~/.autopunchin nach ~/.punchbuddy."""
    if os.path.isdir(_OLD_SETTINGS_DIR) and not os.path.isdir(_NEW_SETTINGS_DIR):
        try:
            shutil.copytree(_OLD_SETTINGS_DIR, _NEW_SETTINGS_DIR)
            logging.info(f"Settings migriert: {_OLD_SETTINGS_DIR} → {_NEW_SETTINGS_DIR}")
        except Exception as e:
            logging.warning(f"Settings-Migration fehlgeschlagen: {e}")


def _deep_merge(default, override):
    """Rekursives Merge: verschachtelte dicts werden tief gemischt; für Listen
    und Skalare gewinnt `override`. So erreichen neu hinzugekommene Default-Keys
    auch alte Settings-Dateien, ohne vorhandene Nutzerwerte zu überschreiben."""
    if isinstance(default, dict) and isinstance(override, dict):
        out = dict(default)
        for k, v in override.items():
            out[k] = _deep_merge(default[k], v) if k in default else v
        return out
    return override


# Template eines Preset-Eintrags – fehlende (neu hinzugekommene) Keys in alten
# gespeicherten Presets werden hieraus aufgefüllt.
_PRESET_TEMPLATE = {"name": "", "rec_a": [], "mon_a": [],
                    "rec_b": [], "mon_b": [], "export": []}


def load_settings():
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = _deep_merge(copy.deepcopy(DEFAULT_SETTINGS), data)
            # Preset-Einträge mit dem Template auffüllen (override gewinnt je Key)
            presets = merged.get("track_presets")
            if isinstance(presets, list):
                merged["track_presets"] = [
                    {**_PRESET_TEMPLATE, **p} if isinstance(p, dict) else p
                    for p in presets
                ]
            return merged
        except Exception as e:
            logging.error(f"Settings laden: {e}")
    return copy.deepcopy(DEFAULT_SETTINGS)

def save_settings(s):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)

