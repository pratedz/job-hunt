"""Synthetic fixture logos for mock mode (M9): simple geometric marks, not real brand logos.
Pure stdlib PNG writer. python3 -I dev/make_logos.py"""
import math, struct, zlib, pathlib

OUT = pathlib.Path(__file__).parent / "fixtures" / "logos"
N = 64

def png(path, px):
    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in px)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

def draw(fn, bg=(255, 255, 255, 255)):
    return [[fn(x + .5, y + .5) or bg for x in range(N)] for y in range(N)]

def hexc(h): return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) + (255,)

MARKS = {
    # name: drawing function (x, y) -> colour or None
    "ring.png": lambda x, y: hexc("e4572e") if 14 < math.hypot(x - 32, y - 32) < 24 else None,
    "bars.png": lambda x, y: hexc("1f6f8b") if 12 < y < 52 and int(x) // 10 % 2 == 1 and 8 < x < 56 else None,
    "tri.png": lambda x, y: hexc("6a4c93") if y > 14 and y < 52 and abs(x - 32) < (y - 14) * 0.6 else None,
    "dot.png": lambda x, y: hexc("ffffff") if math.hypot(x - 32, y - 32) < 9 else hexc("2a9d8f") if math.hypot(x - 32, y - 32) < 26 else None,
    "split.png": lambda x, y: (hexc("f4a261") if x < y else hexc("264653")) if 10 < x < 54 and 10 < y < 54 else None,
    "wide.png": None,  # a wide wordmark-like logo, to test object-fit: contain
}

def wide():
    w, h = 64, 24
    rows = [[(hexc("c1121f") if 3 < y < 21 and (x // 6) % 2 == 0 and 2 < x < 62 else (255, 255, 255, 0)) for x in range(w)] for y in range(h)]
    raw = b"".join(b"\x00" + bytes(c for p in r for c in p) for r in rows)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    (OUT / "wide.png").write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

for name, fn in MARKS.items():
    if fn: png(OUT / name, draw(fn))
wide()
# deliberately broken: PNG signature, then garbage, so the image fails to decode and the UI shows the monogram
(OUT / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"not really a png" * 4)
print(sorted(p.name for p in OUT.iterdir()))
