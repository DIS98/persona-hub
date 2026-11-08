"""把小尺寸图标放大拼成对比图，用于肉眼检查 16/24/32 的可辨识度。

最近邻放大（不插值），因为要看的正是「像素级是否还认得出」，
插值会把问题糊掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from make_icons import png_bytes, render  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "web"


def write_png(path: Path, w: int, h: int, rgba: bytes) -> None:
    import struct
    import zlib

    raw = b"".join(b"\x00" + rgba[y * w * 4 : (y + 1) * w * 4] for y in range(h))

    def chunk(typ: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + typ + data + struct.pack(
            ">I", zlib.crc32(typ + data) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> int:
    sizes = [16, 24, 32, 48]
    scale = 8
    gap = 12
    total_w = sum(s * scale for s in sizes) + gap * (len(sizes) + 1)
    total_h = max(sizes) * scale + gap * 2

    # 中灰底，方便看清图标的浅色边缘
    canvas = bytearray(b"\x2a\x2e\x36\xff" * (total_w * total_h))

    x0 = gap
    for s in sizes:
        rgba = render(s)
        big = s * scale
        for y in range(big):
            for x in range(big):
                sx, sy = x // scale, y // scale
                i = (sy * s + sx) * 4
                r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
                if a == 0:
                    continue
                # 与底色做 alpha 合成
                j = ((gap + y) * total_w + (x0 + x)) * 4
                canvas[j] = (r * a + canvas[j] * (255 - a)) // 255
                canvas[j + 1] = (g * a + canvas[j + 1] * (255 - a)) // 255
                canvas[j + 2] = (b * a + canvas[j + 2] * (255 - a)) // 255
                canvas[j + 3] = 255
        x0 += big + gap

    dst = OUT / "_icon-scales.png"
    write_png(dst, total_w, total_h, bytes(canvas))
    print(f"已生成 {dst}  ({total_w}x{total_h})，从左到右依次是 16 / 24 / 32 / 48 像素的 {scale}x 放大")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
