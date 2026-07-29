#!/usr/bin/env python3
"""Erzeugt die vom manifest.json referenzierten Icon-PNGs (ohne Fremd-Libs).

PunchBuddy-Look: dunkler Hintergrund mit rotem Aufnahme-Punkt. Reine
Standardbibliothek (zlib + struct). Erzeugt jeweils 1x und @2x.
"""
import os
import zlib
import struct

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "com.punchbuddy.control.sdPlugin")

BG = (0x1c, 0x1c, 0x1e)   # fast schwarz
DOT = (0xff, 0x3b, 0x30)  # rot (Aufnahme)


def _png(width, height, rgba_rows):
    """rgba_rows: Liste von bytearray-Zeilen (je width*4 Bytes)."""
    raw = bytearray()
    for row in rgba_rows:
        raw.append(0)          # Filter-Typ 0 (None)
        raw.extend(row)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def render(size, dot=True):
    cx = cy = (size - 1) / 2.0
    r = size * 0.30
    r2 = r * r
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            inside = dot and ((x - cx) ** 2 + (y - cy) ** 2) <= r2
            col = DOT if inside else BG
            row += bytes((col[0], col[1], col[2], 0xff))
        rows.append(row)
    return _png(size, size, rows)


def write(path_noext, base_size, dot=True):
    os.makedirs(os.path.dirname(path_noext), exist_ok=True)
    with open(path_noext + ".png", "wb") as f:
        f.write(render(base_size, dot))
    with open(path_noext + "@2x.png", "wb") as f:
        f.write(render(base_size * 2, dot))
    print("  ✓", os.path.relpath(path_noext + ".png", HERE),
          "(+@2x)")


def main():
    # Plugin-/Kategorie-Icons
    write(os.path.join(PLUGIN, "imgs", "plugin", "category-icon"), 28)
    write(os.path.join(PLUGIN, "imgs", "plugin", "marketplace"), 144)
    # Aktions-Icons
    write(os.path.join(PLUGIN, "imgs", "actions", "command", "icon"), 20)
    write(os.path.join(PLUGIN, "imgs", "actions", "command", "key"), 72)
    print("Icons erzeugt.")


if __name__ == "__main__":
    main()
