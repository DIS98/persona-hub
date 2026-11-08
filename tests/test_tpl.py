"""宿主模板插槽：状态判定、补丁幂等、还原往返、以及判据能否抓到错。

被测对象是**真实的 WorkBuddy 安装目录**（`resources/app.asar.unpacked/resources/templates`），
因为这一层的全部价值就是"对着真实文件判得准"。所以：

  - 每条用例跑完都断言最终状态回到 `patched`，不给宿主留下半成品
  - `revert` 会真的把模板还原一次，紧接着 `patch` 补回来；两步都失败才算红
  - 不需要启动 persona-hub 服务，纯函数调用

用法：
    python tests/test_tpl.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import tplpatch as T  # noqa: E402

PASS = 0
FAIL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
        return True
    FAIL += 1
    FAILS.append(f"{name}  {detail}")
    print(f"  \u2717 {name}  -> {detail}")
    return False


# ---------------------------------------------------------------- A 纯函数判定
def group_a() -> None:
    print("\n[A] 状态判定（纯函数，可造样本）")
    patched = "x\r\n" + "\r\n".join(T.SOUL_SLOT) + "\r\n## USER.md\r\n"
    plain = "{% if WorkspaceIdentityMode %}\r\n## USER.md\r\nPath: {{ UserPath }}\r\n{% endif %}\r\n"

    a = T.analyze_text(T.EXPERT_TPL, patched, len(patched), "aaaaaaaaaaaaaaaa")
    check("有插槽 → patched", a["state"] == "patched", a["state"])
    check("有插槽 → has_soul_slot", a["has_soul_slot"] is True)

    b = T.analyze_text(T.EXPERT_TPL, plain, len(plain), "bbbbbbbbbbbbbbbb")
    check("无插槽 → missing", b["state"] == "missing", b["state"])
    check("无插槽 → 判定不误报 patched", b["has_soul_slot"] is False)

    # 变异：把 SoulContent 改成别的变量名，判据必须报 missing ——
    # 这条就是"看起来像补过、实际没补"的样本。
    fake = patched.replace("{{ SoulContent", "{{ SoulContentX")
    c = T.analyze_text(T.EXPERT_TPL, fake, len(fake), "cccccccccccccccc")
    check("变量名写错 → 仍判 missing（不被前缀骗过）", c["state"] == "missing", c["state"])

    # 只写注释提到插槽、但没有真正的 {{ }}，也必须算没补
    comment_only = plain.replace("## USER.md", "<!-- 这里应该有 SoulContent 插槽 -->\r\n## USER.md")
    d = T.analyze_text(T.EXPERT_TPL, comment_only, len(comment_only), "dddddddddddddddd")
    check("只在注释里提到 → 判 missing", d["state"] == "missing", d["state"])


# ---------------------------------------------------------------- B 真实宿主
def group_b() -> None:
    print("\n[B] 真实宿主模板")
    st = T.status()
    host = st["host"]
    check("能定位宿主安装目录", T._templates_dir().is_dir(), str(T._templates_dir()))
    check("能读到宿主版本", bool(host.get("appVersion")), str(host))

    expert = T._tpl(T.EXPERT_TPL)
    check("专家模板存在", expert.is_file(), str(expert))
    if not expert.is_file():
        return

    raw = expert.read_bytes()
    text = raw.decode("utf-8", "replace")
    check("换行仍是 CRLF（文本模式写会把 \\r\\n 改成 \\n）",
          raw.count(b"\r\n") > 0 and raw.count(b"\n") == raw.count(b"\r\n"),
          f"CRLF={raw.count(b'\r\n')} LF={raw.count(b'\n')}")
    check("没有 BOM", not raw.startswith(b"\xef\xbb\xbf"))
    check("Jinja 块配对",
          len(__import__("re").findall(r"\{%-?\s*if\b", text))
          == len(__import__("re").findall(r"\{%-?\s*endif\b", text)), "if/endif 不等")
    check("仍然有 USER 段", "{{ UserPath }}" in text)
    check("仍然有 identity_context 包裹",
          "<identity_context>" in text and "</identity_context>" in text)

    if "{{ SoulContent" in text:
        i_soul = text.find("## SOUL.md")
        i_user = text.find("## USER.md")
        check("SOUL 段在 USER 段之前", 0 <= i_soul < i_user, f"soul={i_soul} user={i_user}")
        check("带授权句（embody its persona and tone）", T.PERSONA_LINE in text)

    check("status 的 expert.state 与文件一致",
          st["expert"]["state"] == ("patched" if "{{ SoulContent" in text else "missing"),
          st["expert"]["state"])


# ---------------------------------------------------------------- C 补丁
def group_c() -> None:
    print("\n[C] 补丁与还原")
    before = T.status()
    r = T.patch()
    st = T.status()
    if before["expert"]["state"] == "patched":
        check("已补过 → 幂等返回 patched=False", r.get("patched") is False, str(r)[:120])
        check("已补过 → 文件不变", st["expert"]["sha16"] == before["expert"]["sha16"])
    else:
        check("正常补上", r.get("patched") is True, str(r)[:160])
        check("补后状态 patched", st["expert"]["state"] == "patched", st["expert"]["state"])
        check("补后留有备份", Path(str(r.get("backup", ""))).is_file(), str(r.get("backup")))

    # 还原 → 再补，跑通往返；失败也必须在 finally 补回，不给宿主留半成品
    reverted = False
    try:
        rv = T.revert()
        reverted = True
        st2 = T.status()
        check("还原后 missing", st2["expert"]["state"] == "missing", st2["expert"]["state"])
        check("还原源是备份文件", Path(str(rv.get("from", ""))).is_file(), str(rv))
    finally:
        if reverted:
            T.patch()
    st3 = T.status()
    check("复原后回到 patched", st3["expert"]["state"] == "patched", st3["expert"]["state"])


# ---------------------------------------------------------------- D 升级提醒
def group_d() -> None:
    print("\n[D] 升级提醒的判定")
    st = T.status()
    rec = st["record"] or {}
    check("补过之后有记录", bool(rec), str(rec)[:100])
    if rec:
        check("记录里存了宿主版本", bool(rec.get("appVersion")), str(rec.get("appVersion")))
        check("记录里存了备份路径", Path(str(rec.get("backup", ""))).is_file(), str(rec.get("backup")))
        check("当前版本与记录一致 → 不报 stale", st["stale"] is False, str(st["stale"]))
        # 变异：把记录的版本改成别的值，必须被判成 stale 并给出升级提示
        saved = T._read_record()
        try:
            T._write_record({**saved, "appVersion": "0.0.0-old"})
            st2 = T.status()
            check("版本对不上 → 报 stale", st2["stale"] is True, str(st2["stale"]))
            check("版本对不上 → 有升级提示",
                  any("升级" in w for w in st2["warnings"]), str(st2["warnings"])[:160])
        finally:
            T._write_record(saved)
        check("记录已恢复", (T.status()["record"] or {}).get("appVersion") == saved.get("appVersion"))


# ---------------------------------------------------------------- E 契约尾块
def group_e() -> None:
    """尾块的作用是"占住身份段最后一个位置"，所以判据必须查位置，不能只查存在。

    只查 `mark in text` 的用例在"尾块被挪到段首"时会照样通过 —— 那就变成
    "改了但没起作用"的假绿。
    """
    print("\n[E] 契约尾块")
    text = T._tpl(T.EXPERT_TPL).read_text(encoding="utf-8", errors="replace")
    check("尾块存在", T.CONTRACT_MARK in text)
    check("尾块只出现一次", text.count(T.CONTRACT_MARK) == 1, str(text.count(T.CONTRACT_MARK)))

    i_mark = text.find(T.CONTRACT_MARK)
    i_user = text.find(T.ANCHOR_USER)
    i_end = text.find(T.ANCHOR_END)
    check("尾块在 USER 段之后", i_mark > i_user, f"mark={i_mark} user={i_user}")
    check("尾块在 identity_context 之内", i_mark < i_end, f"mark={i_mark} end={i_end}")

    # 三条裁决必须都在，缺一条就等于没定冲突
    for key in ("格式服从人格", "事实服从专家", "边界不外扩"):
        check(f"含裁决「{key}」", key in text)

    # 造"被删掉"的样本：状态必须转成未补，并给出提示
    stripped = text.replace(T.CONTRACT_MARK, "", 1)
    st = T.analyze_text(T.EXPERT_TPL, stripped, len(stripped), "cccccccccccccccc")
    check("删掉尾块 → has_contract=False", st["has_contract"] is False)


def main() -> int:
    print("=" * 56)
    print("宿主模板插槽")
    print("=" * 56)
    group_a()
    group_b()
    group_c()
    group_d()
    group_e()
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
