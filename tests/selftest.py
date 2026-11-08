"""核心自检：备份 / 还原 / 事务 / 幂等。

跑法：python tests/selftest.py
全部在临时目录里进行，不碰真实数据目录。
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ph-selftest-"))
os.environ["PERSONA_HUB_HOME"] = str(TMP / "data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.adapters.base import S_MD_BLOCK, upsert_md_block, strip_md_block  # noqa: E402
from core.txn import Transaction  # noqa: E402
from core.vault import Vault, sha256_bytes  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return cond


def main() -> int:
    vault = Vault(TMP / "vault")
    print(f"临时目录: {TMP}\n")

    # ---------------------------------------------------------------- 1 往返
    print("[1] 快照 → 改写 → 还原，逐字节一致")
    f1 = TMP / "a.txt"
    original = "原始内容\n第二行\n".encode("utf-8")
    f1.write_bytes(original)
    sid = vault.capture([f1], label="t1")
    f1.write_bytes(b"changed!!")
    check("改写后内容确实变了", f1.read_bytes() != original)

    res = vault.restore(sid)
    check("还原报告 ok", all(r.ok for r in res), str(res))
    check("内容逐字节一致", f1.read_bytes() == original, f"got={f1.read_bytes()!r}")

    # ---------------------------------------------------------------- 2 原本不存在
    print("\n[2] 「原本不存在」的文件，还原时应被删除")
    f2 = TMP / "b.txt"
    sid2 = vault.capture([f2], label="t2")   # 此时 b.txt 不存在
    f2.write_bytes(b"I was created later")
    check("先确认文件确实被创建", f2.is_file())
    vault.restore(sid2)
    check("还原后文件被删除", not f2.exists())

    # ---------------------------------------------------------------- 3 去重
    print("\n[3] 内容相同的文件只存一份对象")
    d = TMP / "dup"
    d.mkdir()
    same = b"identical payload" * 100
    paths = []
    for i in range(5):
        p = d / f"f{i}.bin"
        p.write_bytes(same)
        paths.append(p)
    before = vault.stats()["objects"]
    vault.capture(paths, label="dup")
    after = vault.stats()["objects"]
    check("5 个同内容文件只新增 1 个对象", after - before == 1, f"before={before} after={after}")

    # ---------------------------------------------------------------- 4 事务回滚
    print("\n[4] 事务中途失败 → 已写的部分自动回滚")
    ok_file = TMP / "ok.txt"
    bad_path = TMP / "isdir"
    ok_file.write_text("v0", encoding="utf-8")
    bad_path.mkdir()                       # 目标是个目录，写入必然失败

    tx = Transaction(vault)
    tx.stage(ok_file, "v1-changed")
    tx.stage(bad_path, "will fail")
    r = tx.commit(label="rollback-test")
    check("事务报告失败", not r.ok, r.message)
    check("已写的文件被回滚为原内容", ok_file.read_text(encoding="utf-8") == "v0",
          f"got={ok_file.read_text(encoding='utf-8')!r}")

    # ---------------------------------------------------------------- 5 只读文件
    print("\n[5] 目标文件是只读的，仍可被覆盖（破甲在这点上翻过车）")
    ro = TMP / "readonly.txt"
    ro.write_text("locked", encoding="utf-8")
    os.chmod(ro, stat.S_IREAD)
    tx2 = Transaction(vault)
    tx2.stage(ro, "unlocked by hub")
    r2 = tx2.commit(label="readonly-test")
    check("写入成功", r2.ok, r2.message)
    check("内容已更新", ro.read_text(encoding="utf-8") == "unlocked by hub")

    # ---------------------------------------------------------------- 6 幂等
    print("\n[6] 同一份内容重复写入 = 无变更（幂等）")
    body = "人格正文第一段\n\n第二段"
    t0 = "原有内容\n"
    once = upsert_md_block(t0, "p1", body)
    twice = upsert_md_block(once, "p1", body)
    thrice = upsert_md_block(twice, "p1", body)
    check("二次写入与一次相同", once == twice)
    check("三次写入仍相同", twice == thrice)
    check("只出现一个起始标记", once.count("persona-hub:begin") == 1)
    check("只出现一个结束标记", once.count("persona-hub:end") == 1)
    check("原内容仍在", once.startswith("原有内容"))

    # ---------------------------------------------------------------- 7 摘除
    print("\n[7] 摘除注入 → 回到注入前形态")
    stripped, changed = strip_md_block(once)
    check("报告已摘除", changed)
    check("摘除后残留原内容", stripped.strip() == "原有内容", repr(stripped))
    check("标记已清空", "persona-hub" not in stripped)

    # ---------------------------------------------------------------- 8 变更检测
    print("\n[8] 内容未变时不产生写入步骤")
    f8 = TMP / "same.txt"
    f8.write_text("stable", encoding="utf-8")
    tx3 = Transaction(vault)
    tx3.stage(f8, "stable")
    check("changed() 为空", len(tx3.changed()) == 0)
    prev = tx3.preview()
    check("预演标记为 unchanged", prev and prev[0]["action"] == "unchanged", str(prev))

    # ---------------------------------------------------------------- 9 完整性
    print("\n[9] vault 对象被破坏时能检出")
    f9 = TMP / "c.txt"
    f9.write_text("integrity", encoding="utf-8")
    sid9 = vault.capture([f9], label="t9")
    meta = vault.load(sid9)
    sha = meta.entries[0]["sha256"]
    obj = vault._object_path(sha)
    obj.write_bytes(b"corrupted")           # 人为破坏
    from core.vault import sha256_file
    f9.write_text("modified", encoding="utf-8")
    res9 = vault.restore(sid9)
    check("还原被拒绝并报错", not all(r.ok for r in res9), str([r.detail for r in res9]))

    # ---------------------------------------------------------------- 收尾
    print("\n" + "=" * 54)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
