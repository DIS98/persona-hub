"""生成应用图标（SVG / PNG / 多尺寸 ICO）。

不依赖任何第三方库：几何图形用数学直接算，PNG 用 zlib 手写，ICO 用 DIB 手写。
这样在任何机器上都能重现同一套图标。

设计要点：
  · 主体是「中断的环 + 中心核」，环上三道等距缺口代表多个入口汇聚到一个核心
  · 全程圆角/圆弧，没有任何尖角（16px 下尖角会糊成一团）
  · 小尺寸（16/24）自动切换为「实心环 + 大圆点」：那个尺寸下画缺口只会脏掉
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "web"

C_TOP = (0x4B, 0x7B, 0xF5)   # 蓝
C_BOT = (0x8B, 0x5C, 0xF6)   # 紫
WHITE = (255, 255, 255)

SS = 4          # 每像素超采样 4x4，做反锯齿
RING_R = 0.300  # 环半径（归一化）
GAP_HALF = 14   # 缺口半角（度），三段各缺 28°


def _lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _sample(u: float, v: float, detailed: bool):
    """归一化坐标 (u,v) ∈ [0,1]²，返回 (r,g,b,a)。"""
    cx, cy = u - 0.5, v - 0.5

    # 底：超椭圆（squircle），比圆角矩形更耐看，且完全没有尖角
    ax, ay = abs(cx * 2), abs(cy * 2)
    if ax ** 4 + ay ** 4 > 1.0:
        return (0, 0, 0, 0)

    # 渐变：左上偏蓝 → 右下偏紫
    t = max(0.0, min(1.0, u * 0.35 + v * 0.85))
    r, g, b = _lerp(C_TOP, C_BOT, t)

    d = math.hypot(cx, cy)
    ring_w = 0.092 if detailed else 0.118
    dot_r = 0.100 if detailed else 0.132

    if abs(d - RING_R) <= ring_w / 2:
        if not detailed:
            return (*WHITE, 255)
        ang = math.degrees(math.atan2(cy, cx)) % 120.0
        if GAP_HALF <= ang <= 120.0 - GAP_HALF:
            return (*WHITE, 255)
        # 落在缺口里：保留底色，让环看起来是断开的

    if d <= dot_r:
        return (*WHITE, 255)

    return (int(r), int(g), int(b), 255)


def render(size: int) -> bytes:
    """渲染为 RGBA 字节串（每像素 4 字节，行优先）。"""
    detailed = size >= 32
    px = bytearray(size * size * 4)
    inv = 1.0 / size
    n = SS * SS
    for y in range(size):
        for x in range(size):
            acc_r = acc_g = acc_b = acc_a = 0
            for sy in range(SS):
                for sx in range(SS):
                    u = (x + (sx + 0.5) / SS) * inv
                    v = (y + (sy + 0.5) / SS) * inv
                    r, g, b, a = _sample(u, v, detailed)
                    # 按 alpha 加权累加，避免边缘出现黑边
                    acc_r += r * a
                    acc_g += g * a
                    acc_b += b * a
                    acc_a += a
            i = (y * size + x) * 4
            if acc_a == 0:
                px[i : i + 4] = b"\x00\x00\x00\x00"
            else:
                px[i] = min(255, acc_r // acc_a)
                px[i + 1] = min(255, acc_g // acc_a)
                px[i + 2] = min(255, acc_b // acc_a)
                px[i + 3] = min(255, acc_a // n)
    return bytes(px)


def png_bytes(size: int) -> bytes:
    rgba = render(size)
    raw = b"".join(b"\x00" + rgba[y * size * 4 : (y + 1) * size * 4] for y in range(size))

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def dib_bytes(size: int) -> bytes:
    """32 位 BMP（DIB）格式，小尺寸使用——兼容性比 PNG-in-ICO 更稳。"""
    rgba = render(size)
    header = struct.pack(
        "<IiiHHIIiiII",
        40,          # biSize
        size,        # biWidth
        size * 2,    # biHeight（含 AND 掩码，故为 2 倍）
        1,           # biPlanes
        32,          # biBitCount
        0,           # biCompression = BI_RGB
        size * size * 4,
        0, 0, 0, 0,
    )
    # 像素自底向上，BGRA
    pixels = bytearray()
    for y in range(size - 1, -1, -1):
        for x in range(size):
            i = (y * size + x) * 4
            r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
            pixels += bytes((b, g, r, a))
    # AND 掩码：1bpp，全 0 表示「按 alpha 处理」，每行补齐到 4 字节
    row = ((size + 31) // 32) * 4
    mask = b"\x00" * (row * size)
    return header + bytes(pixels) + mask


def ico_bytes(sizes: list[int]) -> bytes:
    entries, blobs = b"", b""
    offset = 6 + 16 * len(sizes)
    for s in sizes:
        blob = png_bytes(s) if s >= 64 else dib_bytes(s)
        w = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(blob), offset + len(blobs))
        blobs += blob
    return struct.pack("<HHH", 0, 1, len(sizes)) + entries + blobs


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="人格中枢">
  <defs>
    <linearGradient id="g" x1="0.12" y1="0" x2="0.88" y2="1">
      <stop offset="0" stop-color="#4B7BF5"/>
      <stop offset="1" stop-color="#8B5CF6"/>
    </linearGradient>
    <clipPath id="squircle">
      <path d="M32 0C8 0 0 8 0 32s8 32 32 32 32-8 32-32S56 0 32 0Z"/>
    </clipPath>
  </defs>
  <g clip-path="url(#squircle)">
    <rect width="64" height="64" fill="url(#g)"/>
    <g fill="none" stroke="#fff" stroke-width="6.2" stroke-linecap="round">
      <path d="M42.6 20.4A15.4 15.4 0 0 1 47.3 32"/>
      <path d="M47.3 32A15.4 15.4 0 0 1 32 47.4"/>
      <path d="M32 47.4A15.4 15.4 0 0 1 16.7 32"/>
      <path d="M16.7 32A15.4 15.4 0 0 1 21.4 20.4"/>
    </g>
    <circle cx="32" cy="32" r="6.6" fill="#fff"/>
  </g>
</svg>
"""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    (OUT / "favicon.svg").write_text(SVG, encoding="utf-8", newline="\n")

    for s in (16, 24, 32, 48, 64, 128, 256, 512):
        (OUT / f"icon-{s}.png").write_bytes(png_bytes(s))

    (OUT / "favicon.ico").write_bytes(ico_bytes([16, 24, 32, 48, 64, 128, 256]))

    print("已生成：")
    for f in sorted(OUT.glob("favicon.*")) + sorted(OUT.glob("icon-*.png")):
        print(f"  {f.name}  {f.stat().st_size:,} B")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
