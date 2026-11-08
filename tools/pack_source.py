"""把源码打成发布用 zip（发布页里"源码包"那个下载）。

存在理由：这份 zip 之前是手敲命令打出来的，所以每次都要重新决定
「哪几个目录进、哪几个不进」，漏一个文件就是"下载源码包跑不起来"。
规则写进代码后，版本号一改就能重打。

用法：
    python tools/pack_source.py                 # 版本号取自 core/paths.py
    python tools/pack_source.py --out <路径>

判据（打完自动核验，不通过直接非零退出）：
    1. 必含 hub.py / core/ / web/ / personas/ —— 少任何一个都跑不起来
    2. 不含 data/ / dist/ / .build/ / __pycache__ —— 用户数据与构建产物不发布
    3. 包内 core/paths.py 的 VERSION 与文件名一致
"""
from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# desktop 必须进包：交付形态是桌面版，只给内核源码等于让别人拿不到外壳，
# 也无法核对"更新装到哪、关窗怎么退"这些只在壳里的逻辑。
INCLUDE_DIRS = ["core", "docs", "desktop", "personas", "tests", "tools", "web"]
INCLUDE_FILES = ["hub.py", "README.md", "run.bat"]
EXCLUDE_PARTS = {"__pycache__", ".build", "dist", "data"}
# 截图证据留在仓库里，但不进源码包：它们是给人看的排查记录，
# 不是运行需要的东西。放进去只会让每发一版都多背 1MB+ 的历史图片。
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".png", ".jpg", ".jpeg", ".webp"}
# 同一类原因：release/ 是构建产物，桌面外壳的 node_modules 更不该进包
EXCLUDE_PARTS_EXTRA = {
    "release", "node_modules", "shots-2026-11-01",
    # resources/ 里是打包时塞进去的内核 exe（9MB），源码包不该带二进制
    "resources",
}


def version() -> str:
    text = (ROOT / "core" / "paths.py").read_text(encoding="utf-8")
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("core/paths.py 里找不到 VERSION")
    return m.group(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    ver = version()
    out = Path(args.out) if args.out else (
        ROOT.parent / "persona-hub-site" / f"persona-hub-{ver}.zip"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    added: list[str] = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in INCLUDE_FILES:
            p = ROOT / name
            if p.is_file():
                z.write(p, name)
                added.append(name)
        for d in INCLUDE_DIRS:
            base = ROOT / d
            if not base.is_dir():
                continue
            for p in sorted(base.rglob("*")):
                if not p.is_file():
                    continue
                rel_parts = set(p.relative_to(ROOT).parts)
                if EXCLUDE_PARTS & rel_parts or EXCLUDE_PARTS_EXTRA & rel_parts:
                    continue
                if p.suffix.lower() in EXCLUDE_SUFFIX:
                    continue
                arc = p.relative_to(ROOT).as_posix()
                z.write(p, arc)
                added.append(arc)

    # ---- 自检
    problems: list[str] = []
    if "hub.py" not in added:
        problems.append("缺 hub.py")
    for need in ("core/", "web/", "personas/"):
        if not any(a.startswith(need) for a in added):
            problems.append(f"缺目录 {need}")
    # 交付形态是桌面版。源码包里少了外壳，别人只能拿到内核 ——
    # 而"更新装到哪、关窗怎么退、抽屉怎么让位"这些全在壳里，缺了没法核对。
    for need in ("desktop/src/main.js", "desktop/src/shell.css",
                 "desktop/src/updater.js", "desktop/package.json"):
        if need not in added:
            problems.append(f"缺桌面外壳文件 {need}")
    bad = [a for a in added
           if EXCLUDE_PARTS & set(Path(a).parts) or EXCLUDE_PARTS_EXTRA & set(Path(a).parts)]
    if bad:
        problems.append(f"混入不该发布的内容：{bad[:5]}")

    with zipfile.ZipFile(out) as z:
        inner = z.read("core/paths.py").decode("utf-8")
    if f'VERSION = "{ver}"' not in inner:
        problems.append("包里的 core/paths.py 版本号与文件名不一致")

    print(f"{out}  版本 {ver}  条目 {len(added)}  大小 {out.stat().st_size:,} 字节")
    if problems:
        for p in problems:
            print("  !! " + p)
        return 1
    print("  自检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
