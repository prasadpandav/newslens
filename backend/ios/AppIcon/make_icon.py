#!/usr/bin/env python3
"""Render the Descry mark to PNG, with no third-party dependencies.

The mark is one ring and one dot — an aperture, which is what the word means:
to descry is to make something out at a distance. It is drawn here rather than
exported from a design tool so the icon can never drift from the mark the
product actually renders in its nav bar and masthead (`.logo .mark` in
web/index.html, `DescryMark` in the iOS app). Change the ratios below and every
size regenerates from the same numbers.

Coverage is computed analytically from the distance to each shape's edge, which
antialiases as cleanly as a rasteriser would for geometry this simple, and
keeps this file free of a build dependency the project does not otherwise have.

    python3 make_icon.py            # writes the three 1024px variants

Sizes and appearances follow Assets.xcassets/AppIcon.appiconset/Contents.json.
"""
import struct
import zlib
from pathlib import Path

SIZE = 1024

# Proportions. The in-UI mark is an 18px ring with a 1.5px stroke and a 6px dot
# (design 1b). An icon is read at a glance and at small sizes, so the stroke is
# set one optical size heavier — D/9 rather than D/12 — while the dot keeps its
# relationship to the ring's opening. This is a deliberate optical size, not a
# second logo.
RING_D = 0.605 * SIZE          # outer diameter of the ring
STROKE = RING_D / 9.0
DOT_D = RING_D / 3.0

PAPER = (0xF7, 0xF4, 0xEE)
INK = (0x17, 0x15, 0x0F)
NIGHT = (0x12, 0x11, 0x0F)
NIGHT_INK = (0xF2, 0xEE, 0xE6)


def _coverage(dist, feather=0.7):
    """1 inside the shape, 0 outside, linear across one pixel of edge."""
    if dist <= -feather:
        return 1.0
    if dist >= feather:
        return 0.0
    return 0.5 - dist / (2.0 * feather)


def render(ground, mark, transparent=False):
    """RGBA bytes for one variant, row-major."""
    cx = cy = SIZE / 2.0
    ring_mid = (RING_D - STROKE) / 2.0     # centre-line radius of the stroke
    half = STROKE / 2.0
    dot_r = DOT_D / 2.0

    gr, gg, gb = ground
    mr, mg, mb = mark
    rows = []
    for y in range(SIZE):
        dy = y + 0.5 - cy
        dy2 = dy * dy
        row = bytearray()
        append = row.extend
        for x in range(SIZE):
            dx = x + 0.5 - cx
            d = (dx * dx + dy2) ** 0.5
            # Signed distance to the annulus: positive outside the stroke.
            a = _coverage(abs(d - ring_mid) - half)
            if a < 1.0:
                a = max(a, _coverage(d - dot_r))
            if transparent:
                append(bytes((mr, mg, mb, int(round(a * 255)))))
            else:
                append(bytes((
                    int(round(gr + (mr - gr) * a)),
                    int(round(gg + (mg - gg) * a)),
                    int(round(gb + (mb - gb) * a)),
                    255)))
        rows.append(bytes(row))
    return rows


def write_png(path, rows):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 9))
           + chunk(b"IEND", b""))
    Path(path).write_bytes(png)
    print(f"{path}  {len(png) / 1024:.0f} KB")


if __name__ == "__main__":
    here = Path(__file__).parent
    out = here.parent / "NewsLens/Assets.xcassets/AppIcon.appiconset"
    write_png(out / "AppIcon-1024.png", render(PAPER, INK))
    write_png(out / "AppIcon-1024-dark.png", render(NIGHT, NIGHT_INK))
    write_png(out / "AppIcon-1024-tinted.png",
              render(NIGHT, (0xFF, 0xFF, 0xFF), transparent=True))
