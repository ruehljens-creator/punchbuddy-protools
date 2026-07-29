from pynput import keyboard
import time

def on_activate():
    print('Global hotkey activated!')

shortcut = '<cmd>+<alt>+<ctrl>+p'
print(f"Testing {shortcut}...")

try:
    h = keyboard.GlobalHotKeys({
        shortcut: on_activate})
    h.start()
    print("Started. Waiting 5s...")
    time.sleep(5)
    h.stop()
    print("Stopped.")
except Exception as e:
    print(f"Error: {e}")
