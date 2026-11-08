#!/usr/bin/env python3
"""导入判据的变异测试：把编码兜底与空文件拦截分别改坏，看 test_import_encoding 会不会报红。

这两条都只值几行代码，最容易被"顺手简化"成一句
`read_text(encoding="utf-8", errors="replace")`——而后果是导入成功、
内容全废、接口 200。没有变异测试，这种退化永远发现不了。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "core" / "persona.py"
TEST = ROOT / "tests" / "test_import_encoding.py"

MUTATIONS = [
    (
        "M1 解码退回 utf-8 + replace（GBK 文件全变方框）",
        '    raw = src.read_bytes()\n'
        '    for enc in ("utf-8-sig", "utf-8"):',
        '    raw = src.read_bytes()\n'
        '    return raw.decode("utf-8", errors="replace"), "UTF-8"\n'
        '    for enc in ("utf-8-sig", "utf-8"):',
    ),
    (
        "M2 不试 GBK（只认 UTF-8）",
        '    try:\n        return raw.decode("gb18030"), "GBK"\n    except UnicodeDecodeError:\n        return raw.decode("utf-8", errors="replace"), "未知编码"',
        '    return raw.decode("utf-8", errors="replace"), "未知编码"',
    ),
    (
        "M3 空文件照收（库里留一条点不开的空人格）",
        '        if not body.strip():\n'
        '            # 空文件导进来会变成一条正文为空的人格，界面上多个点不开的空条目。\n'
        '            raise PersonaError(f"{src.name} 里没有任何正文（0 字），没有可导入的内容")',
        '        pass',
    ),
    (
        "M4 不把解码方式写进 desc（用户无从知道被转过码）",
        '        enc_note = "" if enc == "UTF-8" else f"，按 {enc} 解码"',
        '        enc_note = ""',
    ),
]


def run_tests() -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(TEST)], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    out = (r.stdout or "") + (r.stderr or "")
    line = next((ln.strip() for ln in reversed(out.splitlines()) if "通过" in ln and "项" in ln), "")
    return r.returncode, line or out[-200:]


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
