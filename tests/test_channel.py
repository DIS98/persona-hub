"""通道级行为测试：一个通道只留一份人格，且替换要说得清楚。

为什么单独一个文件
==================
之前的测试都在验「区块能不能正确写入/摘除」，没验过**同一个通道里出现第二份人格**这种情况。
实测踩到了：先注入「三融合」，再注入「代码工程师」，USER.md 里就躺着两份，
宿主一次读到两个身份——用户看到的现象是"注入了新人格，但说话还是旧人格"。

本文件只做一件事：把「同通道唯一」这条语义钉死。
全部用例是**纯函数级**的（compose 返回内容，不落盘），不会碰到真实文件。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.adapters import all_adapters  # noqa: E402
from core.adapters.base import (  # noqa: E402
    md_block,
    parse_blocks,
    replace_all_blocks,
)

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


def main() -> int:
    print("=" * 56)
    print("通道唯一性 / 替换语义")
    print("=" * 56)

    # ------------------------------------------------------------ A 纯函数
    print("\n[A] replace_all_blocks")
    base_text = "用户内容A\n\n" + md_block("old", "旧人格") + "\n用户内容B\n"
    new, replaced = replace_all_blocks(base_text, "new", "新人格")
    ids = [b.persona_id for b in parse_blocks(new)]
    check("只留一份", ids == ["new"], str(ids))
    check("旧人格被点名", replaced == ["old"], str(replaced))
    check("旧正文已清除", "旧人格" not in new)
    check("新正文写入", "新人格" in new)
    check("用户内容未被吃", "用户内容A" in new and "用户内容B" in new)

    # 三份 → 一份
    t = md_block("a", "A") + md_block("b", "B") + md_block("c", "C")
    new, replaced = replace_all_blocks(t, "d", "D")
    check("三份替换成一份", len(parse_blocks(new)) == 1, str(parse_blocks(new)))
    check("被替换三份全部点名", sorted(replaced) == ["a", "b", "c"], str(replaced))

    # 同 id 再写一次：仍然只有一份（幂等）
    once, r1 = replace_all_blocks("", "same", "内容1")
    twice, r2 = replace_all_blocks(once, "same", "内容2")
    check("同 id 重写仍是 1 份", len(parse_blocks(twice)) == 1, str(parse_blocks(twice)))
    check("同 id 重写不算替换", r2 == [], str(r2))
    check("同 id 重写内容已更新", "内容2" in twice and "内容1" not in twice)

    # 空内容 / 只有用户内容
    new, replaced = replace_all_blocks("只有用户内容，没有任何区块", "x", "X")
    check("无区块时直接追加", len(parse_blocks(new)) == 1 and replaced == [], str(replaced))
    check("空通道首次写入", len(parse_blocks(replace_all_blocks("", "x", "X")[0])) == 1)

    # ------------------------------------------------------------ B 真实适配器
    print("\n[B] WorkBuddy 通道（compose 只算不写）")
    wb = next(a for a in all_adapters() if a.id == "workbuddy")
    det = wb.detect()
    chans = {str(t.extra.get("channel")): t for t in det.targets}
    check("四条通道齐全", len(chans) == 4, str(sorted(chans)))

    if det.detected:
        res = wb.compose("code-engineer", "【测试】人格正文")
        check("默认写两条通道（覆盖两种会话）", len(res.files) == 2, str(len(res.files)))

        # 每个被写的通道里，本工具区块只能有 1 个
        for path, content in res.files.items():
            blocks = parse_blocks(content)
            check(f"{path.name} 只有一份人格区块", len(blocks) == 1,
                  str([b.persona_id for b in blocks]))
            check(f"{path.name} 是本次的人格",
                  blocks and blocks[0].persona_id == "code-engineer",
                  str([b.persona_id for b in blocks]))

        # 告警是「有旧人格才报」，所以这里只能验条件成立时的行为，
        # 不能断言"一定有告警"——实机文件里可能只有一份，断言就会随环境漂移。
        # 有旧人格时的点名逻辑由 [A] 组的纯函数用例保证。
        has_other = any(
            b.persona_id != "code-engineer" for b in parse_blocks(
                (det.targets[0].path.read_text(encoding="utf-8", errors="replace")
                 if det.targets and det.targets[0].path.is_file() else "")
            )
        )
        warned = [w for w in res.warnings if "原有" in w]
        check("有旧人格时替换告警点名（无旧人格则跳过）",
              bool(warned) or not has_other, f"has_other={has_other} warn={warned}")

        # 换一个人格再 compose 一次，结果仍只能有一份
        res2 = wb.compose("reviewer", "【测试】评审人格")
        for path, content in res2.files.items():
            check(f"{path.name} 换人格后仍一份",
                  len(parse_blocks(content)) == 1,
                  str([b.persona_id for b in parse_blocks(content)]))

        # 用户原有内容不能被吃：USER.md 的模板正文要还在
        for path, content in res2.files.items():
            if path.name == "USER.md":
                check("USER.md 原有模板正文保留", "About Your Human" in content)

    # ------------------------------------------------------------ C 上限与位置
    print("\n[C] 上限与位置")
    mem = chans.get("workbuddy_memory")
    check("MEMORY.md 通道写前插", mem is not None and mem.insert_at == "prepend")
    check("MEMORY.md 带上限 4000", mem is not None and mem.read_limit == 4000)

    if det.detected:
        # MEMORY.md 不在默认通道里，要显式指定才走那条路径
        big = "字" * 4200
        r = wb.compose("code-engineer", big, channels=["workbuddy_memory"])
        # 判据用语义（超窗口 / 超上限），不要绑死某一句文案——
        # 之前用「超过上限」匹配，文案改成「超过本模板自设的 size_limit」就误报了一次
        has_limit_warn = any(("超窗口" in w or "读取上限" in w) for w in r.warnings)
        check("MEMORY.md 超上限会告警（不静默）", has_limit_warn, str(r.warnings)[:200])

        # 默认通道没有硬上限，但长正文要给建议
        r2 = wb.compose("code-engineer", "字" * 4200)
        check("正文偏长给建议", any("偏长" in w for w in r2.warnings), str(r2.warnings)[:200])
        r3 = wb.compose("code-engineer", "短正文")
        check("短正文不啰嗦", not any("偏长" in w for w in r3.warnings), str(r3.warnings)[:120])

        # 同文件多人格冲突告警：这是"有时行有时不灵"的直接原因，必须报出来
        from core.adapters.base import conflict_warning  # noqa: E402

        one = "<!-- persona-hub:begin id=a -->x<!-- persona-hub:end -->"
        two = one + "\n" + "<!-- persona-hub:begin id=b -->y<!-- persona-hub:end -->"
        check("单人格式不误报", conflict_warning("USER.md", one) == [], str(conflict_warning("USER.md", one)))
        cw = conflict_warning("USER.md", two)
        check("两人格并存要告警", len(cw) == 1, str(cw))
        check("告警里点名是哪几个", "a" in cw[0] and "b" in cw[0], cw[0][:80] if cw else "")
        # 判据用语义不绑文案：这个文件里已经因为绑死文案误报过一次
        check("告警说清后果（会摇摆）",
              bool(cw) and ("有时像" in cw[0] or "互相矛盾" in cw[0]),
              cw[0][:120] if cw else "")

    # ------------------------------------------------------------ 收尾
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
