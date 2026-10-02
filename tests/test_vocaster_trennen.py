"""Vocaster sicher trennen (02.10.2026).

disconnect() schloss das USB-Handle, während der Interrupt-Poller noch in
libusb_interrupt_transfer steckte → SIGSEGV. Jetzt: erst den Poller anhalten,
dann trennen; reagiert er nicht, bleibt das Handle offen.
Kein Test erreicht USB (Fake-libusb)."""
import threading
import time

import vocaster_control as vc


class _Lib:
    def __init__(self, transfer_s=0.05):
        self.calls = []
        self.transfer_s = transfer_s
        self.in_transfer = threading.Event()

    def libusb_interrupt_transfer(self, handle, ep, buf, size, transferred, timeout):
        self.in_transfer.set()
        self.calls.append("transfer")
        time.sleep(self.transfer_s)
        return -7                                   # LIBUSB_ERROR_TIMEOUT

    def libusb_release_interface(self, handle, iface):
        self.calls.append("release")

    def libusb_close(self, handle):
        self.calls.append("close")


def _usb(lib):
    u = vc.VocasterUSB.__new__(vc.VocasterUSB)     # ohne libusb laden
    u._lib = lib
    u._handle = object()
    return u


def test_trennen_haelt_erst_den_poller_an():
    lib = _Lib()
    u = _usb(lib)
    u._start_intr_poller()
    assert lib.in_transfer.wait(2)
    th = u._intr_thread
    u.disconnect()
    assert not th.is_alive()
    assert lib.calls[-2:] == ["release", "close"]
    assert "transfer" not in lib.calls[lib.calls.index("release"):]
    assert u._handle is None


def test_haengender_poller_handle_bleibt_offen(monkeypatch):
    lib = _Lib()
    u = _usb(lib)
    u._intr_stop = threading.Event()
    haengt = threading.Event()
    u._intr_thread = threading.Thread(target=haengt.wait, daemon=True)
    u._intr_thread.start()
    echtes_join = threading.Thread.join
    monkeypatch.setattr(threading.Thread, "join", lambda self, timeout=None: echtes_join(self, 0.05))
    try:
        u.disconnect()
        assert "close" not in lib.calls             # lieber offen lassen als abstürzen
        assert u._handle is not None
    finally:
        haengt.set()


def test_trennen_ohne_poller_und_doppelt():
    lib = _Lib()
    u = _usb(lib)
    u.disconnect()
    u.disconnect()
    assert lib.calls == ["release", "close"]
