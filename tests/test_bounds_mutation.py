#!/usr/bin/env python3
"""窗口位置判据的变异测试：把两处判断改坏，看 test_bounds.js 会不会报红。

为什么值得单列：这段逻辑只有两三条判断，很容易在"顺手简化"时被抹掉，
而抹掉之后的表现是**静默**的——程序照样启动，只是窗口跑到看不见的地方，
没有报错、没有日志，用户只会说"点了没反应"。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "desktop" / "src" / "bounds.js"
TEST = ROOT / "tests" / "test_bounds.js"

MUTATIONS = [
    (
        "M1 不检查位置是否还在屏幕上（副屏拔掉后窗口消失）",
        "    if (seen) return { x: saved.x, y: saved.y, width, height };",
        "    return { x: saved.x, y: saved.y, width, height };",
    ),
    (
        "M2 不把尺寸夹进工作区（小屏上底部按钮点不到）",
        "  const width = Math.max(Math.min(MIN_W, primary.width), Math.min(saved.width, primary.width));\n"
        "  const height = Math.max(Math.min(MIN_H, primary.height), Math.min(saved.height, primary.height));",
        "  const width = saved.width;\n  const height = saved.height;",
    ),
]


def run_tests() -> tuple[int, str]:
    r = subprocess.run(
        ["node", str(TEST)], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    out = (r.stdout or "") + (r.stderr or "")
    line = next((ln.strip() for ln in reversed(out.splitlines()) if "通过" in ln), "")
    return r.returncode, line or out[-160:]


def main() -> int:
    original = SRC.read_text(encoding="utf-8", newline="")
    code, line = run_tests()
    print(f"  基线：{line}")
    if code != 0:
        print("  基线不过，变异无意义")
        return 1

    missed = []
    for name, old, new in MUTATIONS:
        if old not in original:
            print(f"  ✗ {name}：锚点没命中")
            missed.append(name)
            continue
        SRC.write_text(original.replace(old, new, 1), encoding="utf-8", newline="")
        try:
            bad, _ = run_tests()
        finally:
            SRC.write_text(original, encoding="utf-8", newline="")
        if bad == 0:
            print(f"  ✗ {name}：没报红")
            missed.append(name)
        else:
            print(f"  ✓ {name}：报红")

    code, _ = run_tests()
    if code != 0:
        print("  恢复后测试不过，源码可能没还原")
        return 1

    total = len(MUTATIONS)
    print(f"  抓到错 {total - len(missed)}/{total}；问题：{'、'.join(missed) if missed else '无'}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
