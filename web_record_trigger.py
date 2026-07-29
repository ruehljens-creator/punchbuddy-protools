#!/usr/bin/env python3
import ptsl
import time
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

MONITOR_TRACKS = ["ST", "A  IT", "OGA", "Mus", "Spr"]
RECORD_TRACKS = ["ST", "A  IT", "OGA", "Mus", "Spr", "Spr 2"]

def send_f12():
    print("Sende F12...")
    subprocess.run(["osascript", "-e", 'tell application "System Events" to key code 111'])

def disable_preroll():
    print("Schalte Pre-Roll aus (falls aktiv)...")
    # Dieses AppleScript prüft im Options-Menü, ob Pre-Roll einen Haken hat. 
    # Wenn ja, drückt es Cmd+K zum Ausschalten.
    script = """
    tell application "System Events"
        tell process "Pro Tools"
            try
                set preItem to menu item "Pre-Roll" of menu "Options" of menu bar 1
                set chk to value of attribute "AXMenuItemMarkChar" of preItem
                if chk is "✓" then
                    keystroke "k" using command down
                end if
            end try
        end tell
    end tell
    """
    subprocess.run(["osascript", "-e", script])

def run_punch_in_sequence():
    try:
        print("\n--- Neuer Trigger empfangen ---")
        print("Verbinde mit Pro Tools via PTSL...")
        with ptsl.open_engine(company_name="MyCompany", application_name="WebRecordTrigger") as engine:
            
            print(f"Setze Input Monitor AUS für: {MONITOR_TRACKS}")
            engine.set_track_input_monitor_state(MONITOR_TRACKS, False)
            
            print(f"Setze Record Enable AN für: {RECORD_TRACKS}")
            engine.set_track_record_enable_state(RECORD_TRACKS, True)
            
            time.sleep(0.1) # Kurze Pause zur Sicherheit
            send_f12()
            
            print("Warte auf Start der Aufnahme...")
            # Kurz warten, bis der Transport anläuft
            for _ in range(20):
                if str(engine.transport_state()) != "TS_TransportStopped":
                    break
                time.sleep(0.1)
                
            print("Aufnahme läuft. Warte auf Stopp...")
            # Warten, bis der Benutzer stoppt
            while str(engine.transport_state()) != "TS_TransportStopped":
                time.sleep(0.2)
                
            print("Aufnahme gestoppt. Führe Cleanup aus...")
            
            # Preroll ausschalten, falls es an war
            disable_preroll()
            
            print(f"Setze Input Monitor wieder EIN für: {MONITOR_TRACKS}")
            engine.set_track_input_monitor_state(MONITOR_TRACKS, True)
            
            print("--- Durchlauf beendet ---")
            
    except Exception as e:
        print(f"Fehler bei der Ausführung: {e}")

class TriggerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/trigger":
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
            # Skript in einem separaten Thread starten, damit der Streamdeck-Request sofort abgeschlossen ist
            threading.Thread(target=run_punch_in_sequence, daemon=True).start()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Unterdrücke die Standard-HTTP-Logs in der Konsole
        pass

if __name__ == "__main__":
    port = 8899
    server = HTTPServer(("127.0.0.1", port), TriggerHandler)
    print(f"Streamdeck Record Server lauscht auf http://127.0.0.1:{port}/trigger")
    print("Beenden mit Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer beendet.")
