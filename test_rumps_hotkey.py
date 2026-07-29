import rumps
from pynput import keyboard
import threading
import time

class TestApp(rumps.App):
    def __init__(self):
        super().__init__("Test", menu=["Hello"])
        self.listener = None
        # Start listener immediately vs later
        self._start_listener()

    def _start_listener(self):
        def on_trigger():
            print("HOTKEY TRIGGERED")
            rumps.notification("Hotkey", "Triggered", "Success!")
        
        self.listener = keyboard.GlobalHotKeys({'<cmd>+<alt>+<ctrl>+p': on_trigger})
        self.listener.start()
        print("Listener started")

if __name__ == "__main__":
    app = TestApp()
    app.run()
