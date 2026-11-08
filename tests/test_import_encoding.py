#!/usr/bin/env python3
"""导入链路的判据：编码、空文件、落盘内容。

为什么单独一个文件：导入是"外部文件进库"的唯一入口，
它出的错全是静默的——返回 200、界面显示成功、内容却已经废了。
机器判的只有落盘字节，所以这里全部落在"读完文件比对内容"上。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.persona import PersonaLibrary, PersonaError  # noqa: E402

pass_n = 0
fail_n = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global pass_n, fail_n
    if ok:
        pass_n += 1
        print(f"  ✓ {name}")
    else:
        fail_n += 1
        print(f"  ✗ {name}" + (f"  -> {detail}" if detail else ""))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ph-enc-"))
    lib = PersonaLibrary(tmp)

    # A. UTF-8 正常路径不能退化
    print("[A] UTF-8 源文件")
    a = tmp / "src" / "utf8.md"
    a.parent.mkdir(parents=True, exist_ok=True)
    a.write_text("---\nname: 纯 utf8\n---\n这是 UTF-8 正文，含标点。", encoding="utf-8")
    dst = lib.import_file(a)
    got = dst.read_text(encoding="utf-8")
    check("正文汉字完好", "这是 UTF-8 正文，含标点。" in got, got[-60:])
    check("没有替换字符", "\ufffd" not in got, repr(got[-60:]))
    check("UTF-8 来源不写解码说明", "解码" not in got, got[:200])

    # B. GBK 源文件（中文老文件最常见）
    print("[B] GBK 源文件")
    b = tmp / "src" / "gbk.md"
    b.write_bytes("---\nname: 国标编码\ncategory: 导入\n---\n这一段是国标编码写的中文正文。".encode("gbk"))
    dst2 = lib.import_file(b)
    got2 = dst2.read_text(encoding="utf-8")
    check("name 解码正确", "国标编码" in got2, got2[:160])
    check("正文解码正确", "这一段是国标编码写的中文正文。" in got2, got2[-80:])
    check("没有替换字符", "\ufffd" not in got2, repr(got2[-80:]))
    check("desc 标明按 GBK 解码", "GBK" in got2, got2[:220])

    # C. GB18030 扩展字符（GBK 覆盖不到的生僻字）
    print("[C] GB18030 扩展")
    c = tmp / "src" / "gb18030.md"
    c.write_bytes("生僻字：𠮷𠀀".encode("gb18030"))
    try:
        dst3 = lib.import_file(c)
        got3 = dst3.read_text(encoding="utf-8")
        check("扩展区汉字没丢", "𠮷" in got3, repr(got3[-40:]))
    except PersonaError as exc:
        check("扩展区汉字没丢", False, str(exc))

    # D. 空文件必须拒绝，而不是在库里留一条空人格
    print("[D] 空文件")
    d = tmp / "src" / "empty.md"
    d.write_text("", encoding="utf-8")
    before = len(list(lib.root.glob("*.md")))
    try:
        lib.import_file(d)
        check("空文件被拒绝", False, "居然导入成功了")
    except PersonaError as exc:
        check("空文件被拒绝", "正文" in str(exc), str(exc))
    check("空文件没有落盘", len(list(lib.root.glob("*.md"))) == before,
          f"{before} -> {len(list(lib.root.glob('*.md')))}")

    # E. 只有空白的文件同样拒绝
    print("[E] 全空白文件")
    e = tmp / "src" / "blank.md"
    e.write_text("   \n\n\t\n  ", encoding="utf-8")
    try:
        lib.import_file(e)
        check("全空白被拒绝", False, "居然导入成功了")
    except PersonaError:
        check("全空白被拒绝", True)

    # F. 既有保护不能退化：id 穿越仍被兜底在库内
    print("[F] 路径穿越兜底")
    f = tmp / "src" / "trav.md"
    f.write_text("正文", encoding="utf-8")
    out = lib.import_file(f, "../../escaped")
    check("落点仍在库内", out.parent == lib.root.resolve() or out.parent == lib.root,
          f"{out.parent} vs {lib.root}")
    check("没有写出库外的文件", not (tmp.parent / "escaped.md").exists(), str(tmp.parent))

    print(f"\n通过 {pass_n} 项，失败 {fail_n} 项")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
