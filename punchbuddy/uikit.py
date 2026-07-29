"""Geteilte AppKit-Helfer: Main-Thread-Dispatch und Fortschrittsfenster.

Von transport, export, loudness und ui genutzt. Importiert nur
AppKit/Foundation/PyObjCTools (lazy), daher zyklenfrei.
"""
import time
import logging

def _dispatch_main(fn):
    """Schedult fn() threadsicher auf dem Main-Thread (fire-and-forget).

    AppHelper.callAfter (performSelectorOnMainThread:) statt
    NSRunLoop.performBlock_: NSRunLoop ist nicht threadsicher und
    performBlock_ weckt den Run-Loop nicht auf. Exceptions im Block werden
    abgefangen, damit sie nicht in den ObjC-Runtime durchschlagen."""
    def _safe():
        try:
            fn()
        except Exception:
            logging.exception("_dispatch_main: Fehler im Main-Thread-Block")
    try:
        from PyObjCTools import AppHelper
        AppHelper.callAfter(_safe)
    except Exception:
        pass


_prog_win_refs = []  # Hält ObjC-Referenzen am Leben


def _show_progress_win(title):
    """
    Öffnet ein schwebendes Fortschrittsfenster ohne Fokus-Diebstahl.
    Gibt {"update": fn(frac, msg), "close": fn()} zurück.
    orderFront_ statt makeKeyAndOrderFront_ → Fokus bleibt beim aktiven Fenster.
    """
    import AppKit as _AK
    WIN_W, WIN_H = 320, 76

    win_ref = [None]
    lbl_ref = [None]
    bar_ref = [None]

    def _make():
        try:
            rect = _AK.NSMakeRect(0, 0, WIN_W, WIN_H)
            win = _AK.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                rect, _AK.NSWindowStyleMaskTitled, _AK.NSBackingStoreBuffered, False
            )
            win.setTitle_(title)
            win.setLevel_(3)  # NSFloatingWindowLevel – immer sichtbar, kein Fokus
            win.setIgnoresMouseEvents_(True)
            screen = _AK.NSScreen.mainScreen()
            if screen:
                sf = screen.frame()
                win.setFrameOrigin_(_AK.NSMakePoint(
                    sf.size.width - WIN_W - 20,
                    sf.size.height - WIN_H - 56
                ))
            cv = win.contentView()
            lbl = _AK.NSTextField.alloc().initWithFrame_(
                _AK.NSMakeRect(12, WIN_H - 36, WIN_W - 24, 16)
            )
            lbl.setStringValue_("Starte…")
            lbl.setBezeled_(False); lbl.setEditable_(False); lbl.setDrawsBackground_(False)
            lbl.setFont_(_AK.NSFont.systemFontOfSize_(11))
            cv.addSubview_(lbl)
            bar = _AK.NSProgressIndicator.alloc().initWithFrame_(
                _AK.NSMakeRect(12, WIN_H - 58, WIN_W - 24, 12)
            )
            bar.setStyle_(0)
            bar.setIndeterminate_(False)
            bar.setMinValue_(0.0); bar.setMaxValue_(1.0); bar.setDoubleValue_(0.0)
            cv.addSubview_(bar)
            win_ref[0] = win; lbl_ref[0] = lbl; bar_ref[0] = bar
            _prog_win_refs.extend([win, lbl, bar])
            win.orderFront_(None)  # kein makeKeyAndOrderFront_ → kein Fokus-Diebstahl
        except Exception as e:
            logging.debug(f"  Fortschrittsfenster: {e}")

    def update(frac, msg):
        def _do():
            try:
                if lbl_ref[0]: lbl_ref[0].setStringValue_(msg)
                if bar_ref[0]: bar_ref[0].setDoubleValue_(frac)
            except Exception: pass
        _dispatch_main(_do)

    def close():
        def _do():
            try:
                if win_ref[0]:
                    win_ref[0].orderOut_(None)
                    win_ref[0] = None
            except Exception: pass
        _dispatch_main(_do)

    _dispatch_main(_make)
    time.sleep(0.1)
    return {"update": update, "close": close}
