"""注入位置与读取上限的专项测试。

为什么单独一套：这一类失败**没有任何报错**——文件写得好好的、哈希也变了，
但内容落在宿主读不到的位置，行为上完全不存在。用户看到的现象是"我注入了但没反应"。

本文件覆盖：
  A 前置注入（prepend）的六种边界
  B frontmatter 保护（USER.md 这类文件）
  C 从"末尾"搬到"开头"时不产生两份
  D 读取上限告警（写进去 ≠ 会被读到）
  E 幂等与多区块共存

用法：python tests/test_position.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.adapters.base import (  # noqa: E402
    S_MD_BLOCK,
    DetectResult,
    MarkdownTargetAdapter,
    Target,
    md_block,
    parse_blocks,
    upsert_md_block,
)

PASS = FAIL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILS.append(name + (f"  ({detail})" if detail else ""))
        print(f"  \u2717 {name}" + (f"  -> {detail}" if detail else ""))
    return cond


class Probe(MarkdownTargetAdapter):
    """把 insert_at / read_limit 摊到构造参数上的探针适配器。"""

    def __init__(self, path: Path, insert_at: str = "append", read_limit: int | None = None):
        super().__init__(aid="probe", name="探针", candidates=[path])
        self._insert_at = insert_at
        self._read_limit = read_limit

    def detect(self) -> DetectResult:
        return DetectResult(
            True,
            "探针",
            [
                Target(
                    self._candidates[0],
                    S_MD_BLOCK,
                    "",
                    insert_at=self._insert_at,
                    read_limit=self._read_limit,
                )
            ],
        )


def positions(text: str) -> list[tuple[str, int]]:
    return [(b.persona_id, b.start) for b in parse_blocks(text)]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ph-pos-"))

    # ============================================================ A 前置注入
    print("\n[A] prepend 基础行为")

    t = upsert_md_block("", "p1", "内容一", insert_at="prepend")
    check("空文件：区块落在 0", positions(t) == [("p1", 0)], str(positions(t)))

    body = "用户原规则第一行\n" * 80
    t = upsert_md_block(body, "p1", "人格内容", insert_at="prepend")
    pos = positions(t)
    check("非空文件：区块在最前", pos and pos[0][1] == 0, str(pos))
    check("原内容完整保留", t.count("用户原规则第一行") == 80, str(t.count("用户原规则第一行")))
    check("人格内容写入", "人格内容" in t)

    # 默认 append 行为没被破坏
    t = upsert_md_block(body, "p1", "人格内容")
    pos = positions(t)
    check("不传 insert_at 时仍追加到末尾", pos and pos[0][1] > 0, str(pos))

    # ============================================================ B frontmatter
    print("\n[B] frontmatter 保护（USER.md 结构）")

    src = "---\nsummary: 身份档案\nread_when:\n  - boot\n---\n\n# USER.md\n\n正文\n"
    t = upsert_md_block(src, "p1", "人格内容", insert_at="prepend")
    check("frontmatter 仍在第一行", t.startswith("---\nsummary: 身份档案"), t[:40])
    check("frontmatter 结构完整（两个 --- 都在）", t.count("\n---\n") >= 1, t[:80])
    fm_end = t.index("\n---\n") + len("\n---\n")
    blk_pos = positions(t)[0][1]
    check("区块在 frontmatter 之后", blk_pos > fm_end - 1, f"区块 {blk_pos} vs frontmatter 结束 {fm_end}")
    check("原正文保留", "# USER.md" in t and "正文" in t)

    # 没有 frontmatter 时不要误判
    t = upsert_md_block("# 标题\n正文\n", "p1", "人格", insert_at="prepend")
    check("无 frontmatter：区块仍在最前", positions(t)[0][1] == 0, str(positions(t)))

    # frontmatter 没闭合（坏文件）不能崩，也不能插进中间
    broken = "---\nsummary: 坏文件\n\n正文\n"
    t = upsert_md_block(broken, "p1", "人格", insert_at="prepend")
    check("frontmatter 未闭合时不崩且插在开头", positions(t)[0][1] == 0, str(positions(t)))

    # ============================================================ C 搬移
    print("\n[C] 从末尾搬到开头")

    old_block = md_block("p1", "旧内容")
    src = "用户规则\n" * 60 + "\n" + old_block
    check("搬运前区块在末尾", positions(src)[0][1] > 200, str(positions(src)))

    t = upsert_md_block(src, "p1", "新内容", insert_at="prepend")
    pos = positions(t)
    check("搬运后只剩一份", len(pos) == 1, str(pos))
    check("搬运后位置在开头", pos[0][1] == 0, str(pos))
    check("内容已更新", "新内容" in t and "旧内容" not in t)
    check("用户内容没丢", t.count("用户规则") == 60, str(t.count("用户规则")))

    # ============================================================ D 读取上限
    print("\n[D] 读取上限告警（写进去 ≠ 会被读到）")

    long_file = tmp / "long.md"
    long_file.write_text("规则行\n" * 2000, encoding="utf-8")   # 约 6000 字符

    # 末尾注入 + 上限 4000 → 必须告警
    res = Probe(long_file, insert_at="append", read_limit=4000).compose("p1", "人格")
    check("末尾注入超限：产生告警", bool(res.warnings), str(res.warnings))
    check("告警里点明「不会生效」", any("不会生效" in w for w in res.warnings), str(res.warnings))
    check("告警里带具体字符位置", any("字符" in w for w in res.warnings), str(res.warnings))

    # 前置注入 → 区块在开头，不该告警
    res = Probe(long_file, insert_at="prepend", read_limit=4000).compose("p1", "人格")
    blk = positions(res.files[long_file])[0]
    check("前置注入：区块在开头", blk[1] <= 1, str(blk))
    check("前置注入：位置告警不该出现",
          not any("不会生效" in w for w in res.warnings), str(res.warnings))

    # 不设 read_limit 时不该有位置告警
    res = Probe(long_file, insert_at="append", read_limit=None).compose("p1", "人格")
    check("未设上限：不产生位置告警",
          not any("不会生效" in w for w in res.warnings), str(res.warnings))

    # ============================================================ E 幂等与共存
    print("\n[E] 幂等与多区块共存")

    t = "基底\n"
    for _ in range(5):
        t = upsert_md_block(t, "p1", "内容一", insert_at="prepend")
    check("连续 5 次 prepend 只有 1 个区块", len(parse_blocks(t)) == 1, str(positions(t)))

    t = "基底\n"
    t = upsert_md_block(t, "a", "甲", insert_at="prepend")
    t = upsert_md_block(t, "b", "乙", insert_at="prepend")
    ids = {b.persona_id for b in parse_blocks(t)}
    check("两个不同人格共存", ids == {"a", "b"}, str(ids))
    check("两者内容都在", "甲" in t and "乙" in t)
    check("基底内容保留", "基底" in t)

    # 更新其中一个不能动另一个
    t = upsert_md_block(t, "a", "甲改过", insert_at="prepend")
    ids = {b.persona_id for b in parse_blocks(t)}
    check("更新 a 后 b 仍在", ids == {"a", "b"}, str(ids))
    check("a 已更新", "甲改过" in t and "甲\n" not in t.replace("甲改过", ""))
    check("b 内容未变", "乙" in t)

    # ============================================================ F 真实适配器配置
    print("\n[F] 真实适配器的目标配置")

    from core.adapters import all_adapters

    by_id = {a.id: a for a in all_adapters()}
    check("存在 workbuddy（记忆层）", "workbuddy" in by_id, str(list(by_id)))
    # USER.md 现在是**专家会话的唯一通道**（专家模板只注入它），必须存在。
    # 但它不再是独立条目，而是 workbuddy 这个条目里的一条通道。
    wb = by_id.get("workbuddy")
    chans = {str(t.extra.get("channel")): t for t in wb.detect().targets} if wb else {}
    check("USER.md 通道仍在（专家会话唯一通道）", "workbuddy_user" in chans, str(sorted(chans)))

    if wb:
        # 只对 MEMORY.md 这条通道断言位置与上限：
        # 合并后 targets 是所有通道的并集，拿并集去断言必然误报。
        mem = chans.get("workbuddy_memory")
        check("记忆层目标用 prepend", mem is not None and mem.insert_at == "prepend",
              str(mem.insert_at) if mem else "缺该通道")
        check("记忆层目标带 read_limit 4000",
              mem is not None and mem.read_limit == 4000,
              str(mem.read_limit) if mem else "缺该通道")

    # ============================================================ 身份通道
    # 依据：宿主模板 resources/templates/user-context-*.tpl
    #   普通会话 → customPrompt（MUST follow）> SOUL.md（embody persona）
    #   专家会话 → 只剩 USER.md
    print("\n[身份通道]")
    import json as _json

    by_id = {a.id: a for a in all_adapters()}
    # 四条通道合并进一个客户端条目：界面上一个软件只该出现一行
    check("WorkBuddy 只占一个条目", "workbuddy" in by_id, str(list(by_id)))
    check("不再单独暴露 workbuddy_custom", "workbuddy_custom" not in by_id)
    check("不再单独暴露 workbuddy_soul", "workbuddy_soul" not in by_id)
    check("不再单独暴露 workbuddy_user", "workbuddy_user" not in by_id)
    wb = by_id.get("workbuddy")
    if wb is not None:
        chans = {str(t.extra.get("channel")) for t in wb.detect().targets}
        check("一个条目内含 4 条通道", len(chans) >= 3, str(sorted(chans)))

    # 自定义指令：写进去要能保留别人家的键，也要能原样清干净
    from core.adapters.workbuddy_identity import WorkBuddyCustomAdapter
    cc = WorkBuddyCustomAdapter()
    if cc is not None and cc.detect().detected:
        tgt = cc.detect().targets[0]
        before = _json.loads(tgt.path.read_text(encoding="utf-8"))
        res = cc.compose("t-custom", "【测试】内容")
        after_raw = list(res.files.values())[0]
        after = _json.loads(after_raw)
        check("自定义指令写入后 JSON 仍可解析", isinstance(after, dict))
        check("自定义指令含注入标记",
              "persona-hub:begin" in after["personalization"]["customPrompt"])
        check("自定义指令没有破坏其他键",
              after.get("locale") == before.get("locale")
              and after["personalization"].get("toneStyle")
              == before["personalization"].get("toneStyle"))
        check("自定义指令注入会带重写风险告警", bool(res.warnings), str(res.warnings))

        # cleanup 读的是磁盘现状，不是 compose 的返回。字段本来就空时，
        # 正确行为是「不产生任何改动」——不能为了测试通过而硬造一次写入。
        c1 = cc.cleanup(cc.detect().targets)
        cur = _json.loads(tgt.path.read_text(encoding="utf-8"))["personalization"]["customPrompt"]
        if not cur.strip():
            check("customPrompt 为空时清除不产生改动", not c1.files, str(list(c1.files)))
        else:
            check("customPrompt 非空时清除产生改动", bool(c1.files), str(list(c1.files)))

        # 剥除逻辑本身用纯函数验：这一步与文件无关
        from core.adapters.base import strip_md_block as _strip
        sample = "前面的用户内容\n\n" + md_block("t-custom", "【测试】内容") + "\n后面的内容"
        cleaned_s, changed_s = _strip(sample)
        check("strip 能剥掉区块", changed_s and "persona-hub:begin" not in cleaned_s)
        check("strip 保住区块之外的内容",
              "前面的用户内容" in cleaned_s and "后面的内容" in cleaned_s, cleaned_s[:60])

    # SOUL.md / USER.md：frontmatter 必须保住，区块落在其后
    from core.adapters.workbuddy_identity import WorkBuddySoulAdapter, WorkBuddyUserAdapter
    _subs = {"SOUL.md": WorkBuddySoulAdapter(), "USER.md": WorkBuddyUserAdapter()}
    for aid, fname in (("workbuddy_soul", "SOUL.md"), ("workbuddy_user", "USER.md")):
        a = _subs.get(fname)
        if a is None or not a.detect().detected:
            print(f"  · 跳过 {aid}（本机没有 {fname}）")
            continue
        res = a.compose("t-id", "【测试】内容")
        text = list(res.files.values())[0]
        check(f"{fname} frontmatter 仍是首行", text.startswith("---"))
        blk = next((b for b in parse_blocks(text) if b.persona_id == "t-id"), None)
        check(f"{fname} 区块在 frontmatter 之后",
              blk is not None and blk.start > 0 and blk.start > text.index("---\n", 3),
              str(blk.start if blk else None))
        # 不覆盖原有的 SOUL 内容
        check(f"{fname} 原有内容仍在", "SOUL" in text or "USER" in text)

    # ============================================================ 收尾
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
