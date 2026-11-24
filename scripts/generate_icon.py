"""Generate a crisp multi-resolution Windows .ico icon for QQ Antigravity Launcher.

Uses only the Python standard library (struct + zlib) to render an anti-aliased
RGBA rounded-square badge with a chat bubble and four-point Antigravity spark,
packed as PNG-encoded ICO frames (256, 64, 48, 32, 16 px).
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _sdf_rounded_box(px: float, py: float, bx: float, by: float, r: float) -> float:
    qx = abs(px) - bx + r
    qy = abs(py) - by + r
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def _sdf_circle(px: float, py: float, cx: float, cy: float, r: float) -> float:
    return math.hypot(px - cx, py - cy) - r


def _sdf_triangle(
    px: float, py: float,
    x0: float, y0: float,
    x1: float, y1: float,
    x2: float, y2: float,
) -> float:
    e0x, e0y = x1 - x0, y1 - y0
    e1x, e1y = x2 - x1, y2 - y1
    e2x, e2y = x0 - x2, y0 - y2
    v0x, v0y = px - x0, py - y0
    v1x, v1y = px - x1, py - y1
    v2x, v2y = px - x2, py - y2

    def _dot(ax: float, ay: float, bx: float, by: float) -> float:
        return ax * bx + ay * by

    t0 = _clamp(_dot(v0x, v0y, e0x, e0y) / max(_dot(e0x, e0y, e0x, e0y), 1e-9))
    t1 = _clamp(_dot(v1x, v1y, e1x, e1y) / max(_dot(e1x, e1y, e1x, e1y), 1e-9))
    t2 = _clamp(_dot(v2x, v2y, e2x, e2y) / max(_dot(e2x, e2y, e2x, e2y), 1e-9))

    pq0x, pq0y = v0x - e0x * t0, v0y - e0y * t0
    pq1x, pq1y = v1x - e1x * t1, v1y - e1y * t1
    pq2x, pq2y = v2x - e2x * t2, v2y - e2y * t2

    s = 1.0 if (e0x * e2y - e0y * e2x) >= 0 else -1.0
    d = min(
        _dot(pq0x, pq0y, pq0x, pq0y),
        _dot(pq1x, pq1y, pq1x, pq1y),
        _dot(pq2x, pq2y, pq2x, pq2y),
    )
    inside = min(
        s * (v0x * e0y - v0y * e0x),
        s * (v1x * e1y - v1y * e1x),
        s * (v2x * e2y - v2y * e2x),
    )
    return -math.sqrt(d) if inside > 0 else math.sqrt(d)


def _blend(dst: tuple[float, float, float, float], src: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    sr, sg, sb, sa = src
    dr, dg, db, da = dst
    out_a = sa + da * (1.0 - sa)
    if out_a <= 1e-6:
        return (0.0, 0.0, 0.0, 0.0)
    out_r = (sr * sa + dr * da * (1.0 - sa)) / out_a
    out_g = (sg * sa + dg * da * (1.0 - sa)) / out_a
    out_b = (sb * sa + db * da * (1.0 - sa)) / out_a
    return (out_r, out_g, out_b, out_a)


def _sample(u: float, v: float) -> tuple[float, float, float, float]:
    # Normalized coordinates [-1, 1]
    px = u * 2.0 - 1.0
    py = v * 2.0 - 1.0

    # Background rounded square badge
    bg_d = _sdf_rounded_box(px, py, 0.88, 0.88, 0.28)
    bg_a = _clamp(0.5 - bg_d * 48.0)
    if bg_a <= 0.0:
        return (0.0, 0.0, 0.0, 0.0)

    # Diagonal indigo -> cyan-blue gradient
    gt = _clamp((u * 0.65 + v * 0.75) * 0.85)
    r = _lerp(24.0 / 255.0, 15.0 / 255.0, gt)
    g = _lerp(48.0 / 255.0, 118.0 / 255.0, gt)
    b = _lerp(132.0 / 255.0, 198.0 / 255.0, gt)

    # Subtle radial glow in top-left
    glow = math.exp(-((px + 0.35) ** 2 + (py + 0.45) ** 2) * 2.4)
    r = _clamp(r + glow * 0.24)
    g = _clamp(g + glow * 0.28)
    b = _clamp(b + glow * 0.35)

    # Subtle inner border highlight
    border_d = abs(bg_d + 0.03)
    border_a = _clamp(1.0 - border_d * 55.0) * 0.28
    col = (r, g, b, bg_a)
    if border_a > 0:
        col = _blend(col, (0.65, 0.88, 1.0, border_a * bg_a))

    # Chat bubble body (rounded box + bottom-left tail)
    bubble_box = _sdf_rounded_box(px, py + 0.05, 0.54, 0.41, 0.22)
    tail = _sdf_triangle(px, py, -0.36, 0.26, -0.10, 0.34, -0.48, 0.56)
    bubble_d = min(bubble_box, tail)
    bubble_a = _clamp(0.5 - bubble_d * 44.0)
    if bubble_a > 0.0:
        # Soft bright white-blue bubble
        bt = _clamp((py + 0.45) * 0.9)
        br = _lerp(1.0, 0.90, bt)
        bg_c = _lerp(1.0, 0.96, bt)
        bb = 1.0
        col = _blend(col, (br, bg_c, bb, bubble_a * 0.97))

    # Four-point Antigravity AI spark inside the chat bubble
    sx = px - 0.02
    sy = py + 0.06
    # Astroid / 4-point star shape: (|x|^0.55 + |y|^0.55) <= R^0.55
    star_val = (abs(sx) ** 0.58 + abs(sy) ** 0.58)
    star_r = 0.31 ** 0.58
    star_a = _clamp(0.5 - (star_val - star_r) * 26.0)
    if star_a > 0.0:
        st = _clamp((sx + sy + 0.35) * 1.4)
        sr = _lerp(0.18, 0.05, st)
        sg = _lerp(0.38, 0.68, st)
        sb = _lerp(0.96, 0.92, st)
        col = _blend(col, (sr, sg, sb, star_a))

    # Small secondary accent dot in top-right of bubble
    dot_d = _sdf_circle(px, py, 0.30, -0.22, 0.075)
    dot_a = _clamp(0.5 - dot_d * 44.0)
    if dot_a > 0.0:
        col = _blend(col, (0.06, 0.72, 0.60, dot_a))

    return col


def _render_png(size: int) -> bytes:
    samples = 2 if size >= 64 else 3
    rows = bytearray()
    inv_s = 1.0 / samples
    for y in range(size):
        rows.append(0)  # PNG filter type 0 (None)
        for x in range(size):
            r = g = b = a = 0.0
            for sy in range(samples):
                v = (y + (sy + 0.5) * inv_s) / size
                for sx in range(samples):
                    u = (x + (sx + 0.5) * inv_s) / size
                    cr, cg, cb, ca = _sample(u, v)
                    r += cr * ca
                    g += cg * ca
                    b += cb * ca
                    a += ca
            n = samples * samples
            a /= n
            if a > 1e-6:
                r /= (a * n)
                g /= (a * n)
                b /= (a * n)
            rows.extend((
                int(_clamp(r) * 255 + 0.5),
                int(_clamp(g) * 255 + 0.5),
                int(_clamp(b) * 255 + 0.5),
                int(_clamp(a) * 255 + 0.5),
            ))

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(rows), level=9)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def build_ico(output_path: Path, sizes: tuple[int, ...] = (256, 64, 48, 32, 16)) -> None:
    pngs = [(sz, _render_png(sz)) for sz in sizes]
    header = struct.pack("<HHH", 0, 1, len(pngs))
    entries = bytearray()
    offset = 6 + 16 * len(pngs)
    for sz, png_data in pngs:
        dim = 0 if sz >= 256 else sz
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                dim,
                dim,
                0,
                0,
                1,
                32,
                len(png_data),
                offset,
            )
        )
        offset += len(png_data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(header + bytes(entries) + b"".join(p for _, p in pngs))


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "assets" / "app.ico"
    build_ico(out)
    print(f"Generated icon: {out} ({out.stat().st_size} bytes)")
