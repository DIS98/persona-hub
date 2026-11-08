"""边界与异常路径测试。

selftest.py 测的是「正常路径对不对」，这里测的是「不正常的时候会不会崩、会不会搞坏东西」。

覆盖六组：
  A 人格库边界     空正文 / 超长 / 继承环 / 变量 / 非法 id / 重复 id
  B Vault 边界     损坏对象 / 不存在快照 / 空库 GC / 去重 / 原不存在文件
  C 事务边界       空事务 / 删不存在的文件 / 只读目标 / 中途失败回滚
  D 检测边界       空文件 / 非 UTF-8 / 超大文件 / 无 adapter
  E 开关移除边界   重复 toggle / 不存在 id / 结构型拒绝 / muted 丢失
  F 并发           两线程同时操作同一目标

所有写操作都落在临时目录，不碰真实客户端文件。
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ph-edge-"))
os.environ["PERSONA_HUB_HOME"] = str(TMP / "data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.adapters.base import parse_blocks, strip_md_block, upsert_md_block  # noqa: E402
from core.paths import data_root  # noqa: E402
from core.persona import PersonaError, PersonaLibrary  # noqa: E402
from core.txn import Transaction  # noqa: E402
from core.vault import Vault, sha256_file  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return cond


def expect_raises(name: str, fn, exc_types=Exception) -> bool:
    try:
        fn()
    except exc_types as e:
        return check(name, True) and True
    except Exception as e:  # noqa: BLE001
        return check(name, False, f"抛了非预期异常 {type(e).__name__}: {e}")
    return check(name, False, "没有抛异常")


def main() -> int:
    vault = Vault(TMP / "vault")
    lib = PersonaLibrary(TMP / "personas")
    work = TMP / "work"
    work.mkdir(parents=True)

    # ================================================================ A 人格库
    print("[A] 人格库边界")

    p = lib.create("ok-one", "正常人格", "正文内容", "测试")
    check("create 成功", p.is_file())

    expect_raises("重复 id 被拒", lambda: lib.create("ok-one", "重名"), PersonaError)

    def bad_id():
        lib.create("带空格 的 id", "x")

    expect_raises("非法 id 被拒（空格）", bad_id, PersonaError)

    def bad_id2():
        lib.create("a" * 60, "x")

    expect_raises("超长 id 被拒（>48）", bad_id2, PersonaError)

    # 空正文
    empty = lib.root / "empty-body.md"
    empty.write_text("---\nid: empty-body\nname: 空\n---\n\n", encoding="utf-8")
    issues = lib.validate("empty-body", {})
    check("空正文被校验拦下", any("正文为空" in i for i in issues), str(issues))

    # 超长正文：默认不再检查（"9000" 是早年拍的假限制），但显式设了 size_limit 就该拦
    # 断言只认语义字段（size_limit + 具体值），不认文案——文案改了不该让测试假红
    long_md = lib.root / "too-long.md"
    long_md.write_text(
        f"---\nid: too-long\nname: 长\nsize_limit: 100\n---\n\n{'あ' * 200}\n", encoding="utf-8"
    )
    issues = lib.validate("too-long", {})
    check("显式 size_limit 超限被拦下",
          any("size_limit" in i and "100" in i for i in issues), str(issues))

    # 反过来：不写 size_limit 时不检查（默认 0），不该冒出任何超限告警
    free_md = lib.root / "no-limit.md"
    free_md.write_text(
        f"---\nid: no-limit\nname: 无上限\n---\n\n{'あ' * 20000}\n", encoding="utf-8"
    )
    check("未设 size_limit 时不报超限",
          not any("size_limit" in i for i in lib.validate("no-limit", {})),
          str(lib.validate("no-limit", {})))

    # 继承环
    (lib.root / "loop-a.md").write_text("---\nid: loop-a\nname: A\nextends: loop-b\n---\n\n甲\n", encoding="utf-8")
    (lib.root / "loop-b.md").write_text("---\nid: loop-b\nname: B\nextends: loop-a\n---\n\n乙\n", encoding="utf-8")
    expect_raises("继承环被检出", lambda: lib.resolve_chain("loop-a"), PersonaError)

    # 继承不存在的父
    (lib.root / "orphan.md").write_text("---\nid: orphan\nname: O\nextends: not-exist\n---\n\n丙\n", encoding="utf-8")
    expect_raises("父模板不存在时报错", lambda: lib.resolve_chain("orphan"), PersonaError)

    # 变量
    (lib.root / "with-var.md").write_text(
        "---\nid: with-var\nname: 变量\n---\n\n你好 {{ who }}，再说一次 {{who}}\n", encoding="utf-8"
    )
    vp = lib.get("with-var")
    check("变量去重", vp.variables == ["who"], str(vp.variables))
    check("变量替换", "张三" in lib.render("with-var", {"who": "张三"}))
    check("未提供变量保留原样", "{{ who }}" in lib.render("with-var", {}))
    issues = lib.validate("with-var", {})
    check("未提供变量进校验", any("变量未提供" in i for i in issues), str(issues))

    # 不存在的 id
    expect_raises("get 不存在的 id", lambda: lib.get("no-such"), PersonaError)

    # 特殊字符正文。
    # 注意：模板统一按 LF 落盘（写入显式 newline="\n"），读回时 Python 的通用换行
    # 会把 CRLF 规范成 LF。这是刻意行为——模板是我们自己的产物，不需要保真编辑器换行。
    # 真正要求逐字节可逆的只有 muted 存档与 vault 对象，那两处走二进制。
    special = "含 <html> & \"引号\" '单引' emoji 🎯 制表\t换行\n结束"
    (lib.root / "special.md").write_text(f"---\nid: special\nname: 特殊\n---\n\n{special}\n",
                                         encoding="utf-8", newline="\n")
    check("特殊字符原样保留（LF 模板）", special in lib.render("special", {}),
          repr(lib.render("special", {})[-30:]))

    # CRLF 输入会被规范化为 LF —— 记录这个行为，而不是假装它保真
    (lib.root / "crlf.md").write_text("---\nid: crlf\nname: CRLF\n---\n\n甲\r\n乙\n",
                                      encoding="utf-8", newline="")
    rendered = lib.render("crlf", {})
    check("CRLF 模板被规范化为 LF", "\r" not in rendered, repr(rendered))

    # 但 muted 存档必须逐字节可逆（switch 那侧用二进制，单独在 E 组验证）

    # frontmatter 缺失
    (lib.root / "nofm.md").write_text("就是一段正文，没有 frontmatter\n", encoding="utf-8")
    np_ = lib.get("nofm")
    check("无 frontmatter 也能读", "就是一段正文" in np_.body)

    # ---- 中文名不能撞 id（用户实际踩到的 bug）
    # 以前：新建时前端用 name 生成 id，中文全被替换成 "-"，
    # 于是第二个中文名人格拿到同一个 id，直接覆盖第一个。
    from core.server import App

    app = App()
    r1 = app.persona_save("", {"name": "零", "body": "第一个人的内容"}, create=True)
    r2 = app.persona_save("", {"name": "另一个中文名", "body": "第二个人的内容"}, create=True)
    check("两个中文名拿到不同 id", r1["id"] != r2["id"], f"{r1['id']} vs {r2['id']}")
    check("id 不是单个连字符", r1["id"] not in ("-", "--"), repr(r1["id"]))
    check("第一个人的内容没被覆盖",
          app.library.get(r1["id"]).body.strip() == "第一个人的内容",
          repr(app.library.get(r1["id"]).body[:30]))
    check("第二个人的内容正确写入",
          app.library.get(r2["id"]).body.strip() == "第二个人的内容")
    check("两个都真的存在", app.library.exists(r1["id"]) and app.library.exists(r2["id"]))

    # 更新走另一条路：更新不存在的人格应当报错而不是偷偷新建
    try:
        app.persona_save("no-such-persona", {"name": "x", "body": "y"}, create=False)
        check("更新不存在的人格被拒绝", False, "没有抛异常")
    except Exception as exc:  # noqa: BLE001
        check("更新不存在的人格被拒绝", "不存在" in str(exc) or "NOT_FOUND" in str(exc), str(exc))

    # 显式指定已存在的 id 新建 → 服务端改分配，不覆盖
    r3 = app.persona_save(r1["id"], {"name": "第三个", "body": "第三份内容"}, create=True)
    check("指定已占用 id 新建时自动改名", r3["id"] != r1["id"], f"{r3['id']}")
    check("原人格仍完好", app.library.get(r1["id"]).body.strip() == "第一个人的内容")

    # ================================================================ B Vault
    print("\n[B] Vault 边界")

    f1 = work / "a.txt"
    f1.write_bytes(b"content-A")
    sid = vault.capture([f1], label="b1")

    expect_raises("不存在的快照被拒", lambda: vault.load("19990101-000000-xxxxxx"), FileNotFoundError)
    expect_raises("还原不存在的快照被拒", lambda: vault.restore("19990101-000000-xxxxxx"), FileNotFoundError)

    # 空库 GC 不报错
    empty_vault = Vault(TMP / "vault-empty")
    removed, freed = empty_vault.gc()
    check("空库 GC 不崩", removed == 0 and freed == 0, f"{removed}/{freed}")

    # 空快照（无条目）
    sid_empty = vault.capture([], label="b-empty")
    meta = vault.load(sid_empty)
    check("空快照条目为 0", len(meta.entries) == 0)
    check("空快照还原返回空", vault.restore(sid_empty) == [])

    # 同一路径重复 capture
    sid2 = vault.capture([f1], label="b2")
    check("重复 capture 生成不同 id", sid2 != sid)

    # 不存在的路径进 capture → 记为 existed=false
    ghost = work / "never-existed.txt"
    sid3 = vault.capture([ghost], label="b3")
    m3 = vault.load(sid3)
    check("不存在路径记录 existed=false", m3.entries[0]["existed"] is False)

    # 二进制文件往返
    binfile = work / "bin.dat"
    blob = bytes(range(256)) * 40
    binfile.write_bytes(blob)
    sid4 = vault.capture([binfile], label="bin")
    binfile.write_bytes(b"changed")
    vault.restore(sid4)
    check("二进制文件逐字节还原", binfile.read_bytes() == blob)

    # 大文件（2 MB）
    big = work / "big.bin"
    bigdata = os.urandom(2 * 1024 * 1024)
    big.write_bytes(bigdata)
    t0 = time.time()
    sid5 = vault.capture([big], label="big")
    restore_ms = int((time.time() - t0) * 1000)
    big.write_bytes(b"x")
    vault.restore(sid5)
    check("2MB 文件还原正确", big.read_bytes() == bigdata)
    check("2MB 快照耗时可接受（<3000ms）", restore_ms < 3000, f"{restore_ms}ms")

    # verify 能发现外部改动
    fv = work / "v.txt"
    fv.write_text("orig", encoding="utf-8")
    sidv = vault.capture([fv], label="v")
    fv.write_text("changed", encoding="utf-8")
    res = vault.verify(sidv)
    check("verify 检出文件被改", any(r.action == "changed" for r in res), str([(r.action, r.ok) for r in res]))

    # ================================================================ C 事务
    print("\n[C] 事务边界")

    tx = Transaction(vault)
    r = tx.commit(label="empty")
    check("空事务不写盘直接成功", r.ok and r.written == 0, r.message)

    # stage 同一路径两次 → 后盖前
    t2 = Transaction(vault)
    t2.stage(f1, "first")
    t2.stage(f1, "second")
    check("同路径 stage 两次只留最后一次", len(t2.changed()) == 1, str(len(t2.changed())))
    t2.discard()
    check("discard 清空暂存", len(t2.staged()) == 0)

    # 删除不存在的文件
    t3 = Transaction(vault)
    t3.stage(work / "nope.txt", None)
    r3 = t3.commit(label="del-ghost")
    check("删除不存在的文件不报错", r3.ok, r3.message)

    # 只读文件删除
    ro = work / "ro-del.txt"
    ro.write_text("x", encoding="utf-8")
    os.chmod(ro, stat.S_IREAD)
    t4 = Transaction(vault)
    t4.stage(ro, None)
    r4 = t4.commit(label="del-ro")
    check("只读文件也能被删", r4.ok and not ro.exists(), r4.message)

    # 目标父目录不存在 → 自动建
    deep = work / "x" / "y" / "z.txt"
    t5 = Transaction(vault)
    t5.stage(deep, "deep")
    r5 = t5.commit(label="mkdir")
    check("自动创建父目录", r5.ok and deep.read_text(encoding="utf-8") == "deep", r5.message)

    # 中途失败 → 回滚（第一次写的也要回退）
    okf = work / "keep.txt"
    okf.write_text("v0", encoding="utf-8")
    blockdir = work / "isdir"
    blockdir.mkdir()
    t6 = Transaction(vault)
    t6.stage(okf, "v1")
    t6.stage(blockdir, "boom")
    r6 = t6.commit(label="rollback")
    check("中途失败整笔回滚", not r6.ok and okf.read_text(encoding="utf-8") == "v0", r6.message)

    # 内容为空字符串（不是 None）→ 写空文件而非删除
    emptyf = work / "empty.txt"
    emptyf.write_text("something", encoding="utf-8")
    t7 = Transaction(vault)
    t7.stage(emptyf, "")
    r7 = t7.commit(label="empty-write")
    check("空字符串写成空文件（不是删除）", r7.ok and emptyf.exists() and emptyf.read_bytes() == b"", r7.message)

    # ================================================================ D 检测
    print("\n[D] 检测边界")

    import core.detect as det

    empty_file = work / "blank.md"
    empty_file.write_text("", encoding="utf-8")
    check("空文件不产出 finding", det.scan_text("t", "T", empty_file, "") == [])

    # 非 UTF-8 内容（检测层收到的是已 decode 的 str，这里测的是不崩）
    weird = work / "weird.md"
    weird.write_bytes(b"\xff\xfe\x00\x00 binary\x00garbage")
    try:
        txt = weird.read_text(encoding="utf-8", errors="replace")
        findings = det.scan_text("t", "T", weird, txt)
        check("非 UTF-8 文件扫描不崩", isinstance(findings, list))
    except Exception as e:  # noqa: BLE001
        check("非 UTF-8 文件扫描不崩", False, f"{type(e).__name__}: {e}")

    # 注入签名命中
    withsig = work / "sig.md"
    withsig.write_text(
        "前文\n<!-- pojia-yijiantong cloudmem v8.18 -->\n人格\nRAW_JSON_END -->\n后文\n", encoding="utf-8"
    )
    fs = det.scan_text("t", "T", withsig, withsig.read_text(encoding="utf-8"))
    check("cloudmem 签名命中", any(f.extra.get("signature") == "pojia-cloudmem" for f in fs), str(len(fs)))
    cloud = next((f for f in fs if f.extra.get("signature") == "pojia-cloudmem"), None)
    if cloud:
        check("区块含起止边界", cloud.off_end > cloud.off_start)
        check("区块标记可开关", cloud.toggleable is True)
    check("文本特征不可删", all(not f.deletable for f in fs if f.extra.get("signature") in ("pojia-selfcheck", "pojia-receipt")))

    # 本工具标记
    selfmd = work / "self.md"
    selfmd.write_text(upsert_md_block("原内容\n", "pid1", "人格正文"), encoding="utf-8")
    fs2 = det.scan_text("t", "T", selfmd, selfmd.read_text(encoding="utf-8"))
    check("本工具区块被识别为 self", any(f.kind == "self" for f in fs2), str([f.kind for f in fs2]))
    check("原内容仍在文件里", "原内容" in selfmd.read_text(encoding="utf-8"))

    # ================================================================ E 开关/移除
    print("\n[E] 开关与移除边界")

    # 用 custom target 隔离：所有写操作落在 TMP
    cdir = TMP / "custom"
    cdir.mkdir()
    cfile = cdir / "target.md"
    cfile.write_text("用户原有内容\n", encoding="utf-8")
    (data_root()).mkdir(parents=True, exist_ok=True)
    (data_root() / "custom-targets.json").write_text(
        json.dumps([{"id": "tmp-target", "name": "临时目标", "paths": [str(cfile)]}], ensure_ascii=False),
        encoding="utf-8",
    )

    import core.switch as sw

    # 先注入一个自己的人格到该目标
    body = lib.render("ok-one")
    t8 = Transaction(vault)
    t8.stage(cfile, upsert_md_block(cfile.read_text(encoding="utf-8"), "ok-one", body))
    r8 = t8.commit(label="seed")
    check("种子注入成功", r8.ok, r8.message)

    items = [f for f in det.scan_all() if f.path.replace("\\", "/").endswith("custom/target.md")]
    check("扫描到临时目标里的注入", len(items) == 1, str(len(items)))

    if items:
        fid = items[0].id
        h_on = sha256_file(cfile)

        # 连续 toggle 5 次，应始终能进能出
        ok_seq = True
        for i in range(5):
            enable = (i % 2 == 1)
            try:
                sw.toggle(vault, lib, fid, enable)
            except Exception as e:  # noqa: BLE001
                ok_seq = False
                check(f"连续 toggle 第 {i + 1} 次", False, f"{type(e).__name__}: {e}")
                break
        check("连续 toggle 5 次不崩", ok_seq)
        # 最终状态：i=4 → enable=False → 停用
        cur = [f for f in det.scan_all() if f.path.replace("\\", "/").endswith("custom/target.md")]
        check("toggle 后状态为停用", cur and cur[0].state == "off", str([c.state for c in cur]))
        # 再开回来，内容应一致
        if cur:
            sw.toggle(vault, lib, cur[0].id, True)
            check("重新启用后内容还原", sha256_file(cfile) == h_on, f"{sha256_file(cfile)[:12]} vs {h_on[:12]}")

        # 不存在的 id
        expect_raises("toggle 不存在的 id 报错", lambda: sw.toggle(vault, lib, "deadbeefdeadbeef", True))

        # 移除
        cur2 = [f for f in det.scan_all() if f.path.replace("\\", "/").endswith("custom/target.md")]
        if cur2:
            rr = sw.remove(vault, [cur2[0].id])
            check("移除成功", rr["ok"] and rr["removed"] == 1, str(rr))
            after = cfile.read_text(encoding="utf-8")
            check("移除后不再有标记", "persona-hub:begin" not in after)
            check("移除后保留用户原有内容", "用户原有内容" in after, repr(after[:60]))

        # 重复移除同一个 id → 应跳过而非崩
        rr2 = sw.remove(vault, [cur2[0].id] if cur2 else ["x"])
        check("重复移除被跳过", rr2["ok"] and rr2["removed"] == 0, str(rr2))

    # 空 ids 应被拒绝而不是静默成功
    expect_raises("空 id 列表被拒", lambda: sw.remove(vault, []))

    # ---- CRLF 内容停用再启用必须逐字节一致
    # 这一条直接验证本轮修掉的 bug：muted 存档走文本模式时，\r\n 会被写成 \r\r\n，
    # 恢复出来的内容就跟原文不一样了——而对客户端配置来说，那是"改坏"。
    crlf_text = (
        "# 头\r\n"
        "<!-- pojia-yijiantong cloudmem v8.18 -->\r\n"
        "<!-- unlock-v6:h=a92b9e681f84 -->\r\n"
        "人格正文第一行\r\n人格正文第二行\r\n"
        "RAW_JSON_END -->\r\n"
        "尾\r\n"
    )
    cf = work / "crlf-mute.md"
    cf.write_bytes(crlf_text.encode("utf-8"))
    cfs = [x for x in det.scan_text("t", "T", cf, crlf_text) if x.extra.get("signature") == "pojia-cloudmem"]
    if check("CRLF 文件能识别出 cloudmem 块", len(cfs) == 1, str(len(cfs))) and cfs:
        cf_f = cfs[0]
        muted_text = sw._mute(crlf_text, cf_f)
        check("停用后正文已移出", "人格正文第一行" not in muted_text)
        check("停用后留了 muted 标记", "persona-hub:muted" in muted_text)
        restored = sw._unmute(muted_text, cf_f)
        check("CRLF 内容停用再启用逐字节一致", restored == crlf_text,
              f"len {len(restored)} vs {len(crlf_text)}；差异片段 "
              f"{repr([a for a, b in zip(restored, crlf_text) if a != b][:3])}")
        check("恢复后 CRLF 未被改写为 LF", "\r\n" in restored and "\r\r\n" not in restored,
              repr(restored[:40]))

    # ---- 多人格共存：不同 id 的人格注入同一目标，应各占一个区块、互不覆盖
    mpath = cdir / "multi.md"
    mpath.write_text("用户原有内容\n", encoding="utf-8")
    (data_root() / "custom-targets.json").write_text(
        json.dumps(
            [
                {"id": "tmp-target", "name": "临时目标", "paths": [str(cfile)]},
                {"id": "tmp-multi", "name": "多人格目标", "paths": [str(mpath)]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    p1 = app.persona_save("", {"name": "甲", "body": "甲的内容 AAAA"}, create=True)
    p2 = app.persona_save("", {"name": "乙", "body": "乙的内容 BBBB"}, create=True)
    # 这一组验的是「显式共存」：默认是一个通道只留一份，共存要显式开
    app.apply({"persona_id": p1["id"], "adapters": ["tmp-multi"], "keep_others": True})
    app.apply({"persona_id": p2["id"], "adapters": ["tmp-multi"], "keep_others": True})

    mt = mpath.read_text(encoding="utf-8")
    check("两个人格各占一个区块", mt.count("persona-hub:begin") == 2, f"{mt.count('persona-hub:begin')} 个")
    check("甲的内容还在", "甲的内容 AAAA" in mt)
    check("乙的内容还在", "乙的内容 BBBB" in mt)
    check("用户原有内容未被吃掉", "用户原有内容" in mt)

    # 再注入一次甲：内容没变，不应新增区块（共存模式下重放同一人格）
    app.apply({"persona_id": p1["id"], "adapters": ["tmp-multi"], "keep_others": True})
    mt2 = mpath.read_text(encoding="utf-8")
    check("重复注入不新增区块", mt2.count("persona-hub:begin") == 2, f"{mt2.count('persona-hub:begin')} 个")
    check("重复注入后乙仍完好", "乙的内容 BBBB" in mt2)

    # 两人格都能被扫描识别
    found = [f for f in det.scan_all() if f.path.replace("\\", "/").endswith("custom/multi.md")]
    check("扫描能识别出两个人格区块", len(found) == 2, f"{len(found)} 个")
    check("两个区块的 persona_id 不同",
          len({f.persona_id for f in found}) == 2,
          str([f.persona_id for f in found]))

    # 默认是「并存」；要"只留一份"必须显式 keep_others=False。
    # 默认值改过一次：原来默认替换，用户实测报"注入了新的人格之后，旧的人格又被覆盖了"。
    app.apply({"persona_id": p2["id"], "adapters": ["tmp-multi"], "keep_others": False})
    mt3 = mpath.read_text(encoding="utf-8")
    check("显式替换时只留一份", mt3.count("persona-hub:begin") == 1, f"{mt3.count('persona-hub:begin')} 个")
    check("显式替换时留下的是最后注入的", "乙的内容 BBBB" in mt3 and "甲的内容 AAAA" not in mt3)

    # 反过来：不传 keep_others 时必须保留（默认并存）
    app.apply({"persona_id": p1["id"], "adapters": ["tmp-multi"]})
    app.apply({"persona_id": p2["id"], "adapters": ["tmp-multi"]})
    mt4 = mpath.read_text(encoding="utf-8")
    check("默认并存：两个人格都在", mt4.count("persona-hub:begin") == 2, f"{mt4.count('persona-hub:begin')} 个")
    check("默认并存：旧的那份内容没丢", "甲的内容 AAAA" in mt4 and "乙的内容 BBBB" in mt4)

    # 结构型条目不可开关（用真实 DSH profile 的条目，只读检查不实际操作）
    structs = [f for f in det.scan_all() if f.extra.get("signature") == "pojia-guard" or f.scope == "dir"]
    if structs:
        check("结构型条目 toggleable=False", all(not f.toggleable for f in structs), str([(f.extra.get('signature'), f.toggleable) for f in structs]))
    else:
        check("结构型条目 toggleable=False（本机无样本，跳过）", True)

    # ================================================================ F 并发
    print("\n[F] 并发")

    conc = work / "conc.md"
    conc.write_text("起点\n", encoding="utf-8")
    errors: list[str] = []

    def worker(tag: str) -> None:
        try:
            t = Transaction(vault)
            t.stage(conc, f"{tag}\n")
            t.commit(label=f"conc-{tag}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{tag}: {type(e).__name__}: {e}")

    ths = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(6)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=20)

    check("并发写入不抛异常", not errors, str(errors[:2]))
    final = conc.read_text(encoding="utf-8")
    check("并发后文件未损坏（能读出完整一行）", final.strip().startswith("w"), repr(final))
    # 每次并发写都应留下快照
    snaps = [m for m in vault.list_snapshots() if m.label.startswith("conc-")]
    check("并发各次都留了快照", len(snaps) >= 4, f"{len(snaps)} 个")

    # ---------------------------------------------------------------- 收尾
    print("\n[G] 重复往返不漂移")

    cycl = work / "cycle.md"
    orig = "# 标题\n内容一\n内容二\r\n含CRLF的一行\n".encode("utf-8")
    cycl.write_bytes(orig)
    drift_at = None
    for i in range(10):
        t = Transaction(vault)
        t.stage(cycl, f"round-{i}\n")
        r = t.commit(label=f"cycle-{i}")
        if not r.ok:
            drift_at = f"第 {i + 1} 轮提交失败: {r.message}"
            break
        vault.restore(r.snapshot_id)
        if cycl.read_bytes() != orig:
            drift_at = f"第 {i + 1} 轮后内容漂移: {cycl.read_bytes()!r}"
            break
    check("10 轮「写入+还原」后逐字节一致", drift_at is None, str(drift_at))

    # 临时文件不能残留
    leftover = [p.name for p in work.rglob("*.hub-tmp-*")] + [p.name for p in work.rglob("*.tmp")]
    check("没有残留临时文件", not leftover, str(leftover[:5]))

    # vault 里不能堆积无引用的孤儿对象（10 轮各写不同内容，对象数应远小于轮数×2）
    stats = vault.stats()
    check("对象数未爆炸式增长", stats["objects"] < 60, f"{stats['objects']} 个对象")

    # 快照能正常列出且按时间倒序
    snaps = vault.list_snapshots()
    check("快照列表非空", len(snaps) > 0)
    times = [m.created_at for m in snaps]
    check("快照按时间倒序", times == sorted(times, reverse=True))

    # GC 后仍能还原最近快照
    if snaps:
        last = snaps[0]
        vault.gc(keep=5)
        res = vault.restore(last.id)
        check("GC 后最近快照仍可还原", all(r.ok for r in res), str([(r.action, r.ok) for r in res][:3]))

    # ---------------------------------------------------------------- 收尾
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILED:
        print("失败清单：")
        for n in FAILED:
            print("  ·", n)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
