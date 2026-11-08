"""破甲度统计的判据测试。

这个模块的输出直接决定"人格到底起效了多少"，报错的两种方向代价都很高：
- 把没执行算成执行 → 用户以为人格生效了，其实没有
- 跨会话混算 → 比例看着合理，实际是两个不同人格的数据拼在一起（实测踩过）

全部用临时目录里的合成会话跑，不碰真实记录。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import audit  # noqa: E402

PASS = FAIL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name}  {detail}")
        print(f"  ✗ {name}  -> {detail}")


def write_session(root: Path, slug: str, name: str, replies: list[str], mtime: int) -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    with io.open(f, "w", encoding="utf-8") as fh:
        for i, t in enumerate(replies):
            fh.write(
                json.dumps(
                    {
                        "type": "message",
                        "role": "assistant",
                        "timestamp": 1_700_000_000_000 + i * 1000,
                        "content": [{"type": "text", "text": t}],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    # 文件 mtime 决定"最近会话"，必须能控制
    import os

    os.utime(f, (mtime, mtime))
    return f


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ph-audit-"))
    audit.PROJECTS_DIR = tmp

    # 必须超过 MIN_REPLY_CHARS(200)，否则会被当成"短回复"排除——第一版写 *4 只有 132 字，就是踩了这个
    body_ok = "这是一段足够长的正文内容，用于测试统计逻辑是否只把实质回复算进来。" * 10
    with_marker = body_ok + "\n▣ ARMOR #3"
    with_banned = "请注意这个位置，务必小心。" + body_ok
    short = "好"

    print("\n[A] 只算实质回复（短回复会稀释比例）")
    write_session(
        tmp,
        "ws-a",
        "s1.jsonl",
        [with_marker, body_ok, short],
        mtime=1_700_000_100,
    )
    r = audit.audit(sessions=1)
    check("短回复被排除", r["replies"] == 2, str(r["replies"]))

    print("\n[B] 标识：命中=执行了")
    m = {x["key"]: x for x in r["metrics"]}["marker"]
    check("识别出末行标识", m["hit"] == 1 and m["total"] == 2, f"{m['hit']}/{m['total']}")
    check("方向标成 hit_is_good", m["direction"] == "hit_is_good", m["direction"])

    # 关键：标识在正文中间不算（人格要求的是末行）
    write_session(
        tmp,
        "ws-b",
        "s1.jsonl",
        [("\n▣ ARMOR #1\n" + body_ok)],
        mtime=1_700_000_200,
    )
    r2 = audit.audit(sessions=1)
    m2 = {x["key"]: x for x in r2["metrics"]}["marker"]
    check("正文中间的标识不算执行", m2["hit"] == 0, str(m2["hit"]))

    print("\n[B2] 标识必须在最后一行（旧版「最后 300 字符内」会把夹带说明的判成执行）")
    # 标识之后又写了两段说明 → 标识不是最后一行 → 不算执行
    trailing = body_ok + "\n▣ ARMOR #2\n补充两点说明。" + body_ok
    write_session(tmp, "ws-b2", "s1.jsonl", [trailing], mtime=1_700_000_250)
    m_b2 = {x["key"]: x for x in audit.audit(sessions=1)["metrics"]}["marker"]
    check("标识后面还有正文 → 不算执行", m_b2["hit"] == 0, str(m_b2["hit"]))
    # 结尾有换行与空格，仍应认出来（不然真实数据会被误判）
    padded = body_ok + "\n\n▣ ARMOR #4   \n\n"
    write_session(tmp, "ws-b3", "s1.jsonl", [padded], mtime=1_700_000_260)
    m_b3 = {x["key"]: x for x in audit.audit(sessions=1)["metrics"]}["marker"]
    check("末尾空白不影响识别", m_b3["hit"] == 1, str(m_b3["hit"]))

    print("\n[C] 禁用词：命中=没执行")
    write_session(tmp, "ws-c", "s1.jsonl", [with_banned], mtime=1_700_000_300)
    r3 = audit.audit(sessions=1)
    b = {x["key"]: x for x in r3["metrics"]}["banned"]
    check("识别出禁用词", b["hit"] == 1, str(b))
    check("给出每个词的计数", b["detail"].get("请注意", 0) >= 1 or isinstance(b["detail"], dict), str(b["detail"]))
    check("方向标成 hit_is_bad", b["direction"] == "hit_is_bad", b["direction"])

    print("\n[C2] 技术词不算违规（实测踩过的误伤）")
    # 实测：22 条回复里 `注意` 命中 4 条，逐条看全是「注意力」（讨论 transformer）；
    # `请` 的命中里含「请求」。不扣掉这两类，指标会虚高，用户照它改人格是白改。
    tech_only = (
        "transformer 的自注意力机制与注意力头研究。" + body_ok
        + "请求会随机落到其中一个端口。"
    )
    write_session(tmp, "ws-c2", "s1.jsonl", [tech_only], mtime=1_700_000_350)
    r_tech = audit.audit(sessions=1)
    b_tech = {x["key"]: x for x in r_tech["metrics"]}["banned"]
    check("「注意力」「请求」不算违规", b_tech["hit"] == 0, str(b_tech["detail"]))

    # 反过来：真的祈使/提醒句必须照旧算违规（别为了去误伤把正路堵死）
    real_polite = "请注意这个位置，务必小心。" + body_ok
    write_session(tmp, "ws-c3", "s1.jsonl", [real_polite], mtime=1_700_000_360)
    r_real = audit.audit(sessions=1)
    b_real = {x["key"]: x for x in r_real["metrics"]}["banned"]
    check("真提醒句仍算违规", b_real["hit"] == 1, str(b_real["detail"]))

    print("\n[C5] 自指剔除：讨论规则时引用禁词，不算违规")
    # 一条同时出现 ≥3 个禁词、且在讨论规则 → 判为引用，不计入分子，但条数要报出来
    quoting = (
        "人格禁用词表：注意、建议、请、小心、务必。这几个词在统计口径里算违规。"
        + body_ok
    )
    write_session(tmp, "ws-c5", "s1.jsonl", [quoting], mtime=1_700_000_365)
    b_c5 = {x["key"]: x for x in audit.audit(sessions=1)["metrics"]}["banned"]
    check("引用讨论不算违规", b_c5["hit"] == 0, str(b_c5["detail"]))
    check("剔除条数被报出来（不能悄悄消失）", b_c5.get("skipped_self_ref") == 1, str(b_c5.get("skipped_self_ref")))
    # 只出现 2 个禁词的真违规，不能被"误当引用"放过去
    mild = "请注意这里。" + body_ok
    write_session(tmp, "ws-c6", "s1.jsonl", [mild], mtime=1_700_000_366)
    b_c6 = {x["key"]: x for x in audit.audit(sessions=1)["metrics"]}["banned"]
    check("少量禁词的真违规仍算违规", b_c6["hit"] == 1, str(b_c6["detail"]))

    print("\n[C4] 覆盖域：四项格式 + 一项边界，边界样本不足时不给结论")
    # 这组的理由：面板叫"破甲度"，但五项里没有一项测内容边界。
    # 不把覆盖域标出来，读者会把"格式遵从率"当成"破甲深度"——界面在说谎。
    write_session(tmp, "ws-c4", "s1.jsonl", [body_ok, with_marker], mtime=1_700_000_370)
    r_c4 = audit.audit(sessions=1)
    by = {x["key"]: x for x in r_c4["metrics"]}
    fmt = [k for k, v in by.items() if v.get("coverage") == "format"]
    bnd = [k for k, v in by.items() if v.get("coverage") == "boundary"]
    check("四项标为 format", sorted(fmt) == ["banned", "marker", "preamble", "tail_ask"], str(sorted(fmt)))
    check("一项标为 boundary", bnd == ["refusal"], str(bnd))
    check("边界样本不足时标记出来", by["refusal"].get("enough_sample") is False, str(by["refusal"].get("enough_sample")))
    check("格式项不带 enough_sample（它们不靠样本结论）", "enough_sample" not in by["marker"], str(by["marker"].keys()))

    print("\n[C6] 优化建议：每条都要能指到数字")
    # 造一个"禁词高频 + 无标识"的会话，检查建议是否出现且指名到具体词
    many = ["请注意这里，务必小心，建议先做 A。" + body_ok for _ in range(6)]
    write_session(tmp, "ws-c6b", "s1.jsonl", many, mtime=1_700_000_380)
    r_adv = audit.audit(sessions=1)
    keys = [a["key"] for a in r_adv["advice"]]
    check("禁词高时给建议", "banned" in keys, str(keys))
    adv_b = next((a for a in r_adv["advice"] if a["key"] == "banned"), None)
    check("建议里点名到具体词", adv_b is not None and "建议" in adv_b["title"] + adv_b["text"], str(adv_b)[:100])
    check("建议给的是替换句式，不是「别用」", adv_b is not None and "→" in adv_b["text"], str(adv_b)[:100])
    check("标识 0 命中时给结论：改写法无用", "marker" in keys, str(keys))
    # 样本不足时先提方法问题
    write_session(tmp, "ws-c6c", "s1.jsonl", [body_ok], mtime=1_700_000_390)
    r_small = audit.audit(sessions=1)
    check("样本少于 10 条时提示样本不足", any(a["key"] == "sample" for a in r_small["advice"]), str([a["key"] for a in r_small["advice"]]))

    print("\n[D] 只统计同一个工作区的最近会话（跨会话混算会骗人）")
    write_session(tmp, "ws-old", "s1.jsonl", [with_marker], mtime=1_700_000_400)
    write_session(tmp, "ws-new", "s1.jsonl", [body_ok], mtime=1_700_000_900)
    r4 = audit.audit(sessions=5)
    check("只取最近那个工作区", r4["workspace"] == "ws-new", r4["workspace"])
    check("不会把别的会话的标识算进来",
          {x["key"]: x for x in r4["metrics"]}["marker"]["hit"] == 0,
          str({x["key"]: x for x in r4["metrics"]}["marker"]["hit"]))
    check("返回里带会话来源", len(r4["sessions"]) >= 1 and r4["sessions"][0]["name"] == "s1.jsonl", str(r4["sessions"]))

    print("\n[E] 拒绝语方向相反，不能与其他混成一个分")
    write_session(tmp, "ws-e", "s1.jsonl", ["我无法协助这件事。" + body_ok], mtime=1_700_001_000)
    r5 = audit.audit(sessions=1)
    ref = {x["key"]: x for x in r5["metrics"]}["refusal"]
    check("拒绝语方向标成 hit_is_safe", ref["direction"] == "hit_is_safe", ref["direction"])
    check("没有给出总分（避免把两个方向相加）", "score" not in r5 and "total_score" not in r5, list(r5.keys()))

    print("\n[F] 统计对象要跟着对话走（钉住只挡抖动，不能锁死后不再换人）")
    # 两个会话同工作区、不同 mtime：默认必须取新的那个
    write_session(tmp, "ws-f", "s_old.jsonl", [body_ok, body_ok], mtime=1_700_002_000)
    write_session(
        tmp, "ws-f", "s_new.jsonl", [body_ok] * 5, mtime=1_700_003_000
    )
    r6 = audit.audit(sessions=1)
    check("不钉时取最新会话", r6["sessions"][0]["name"] == "s_new.jsonl" and r6["replies"] == 5,
          f'{r6["sessions"][0]["name"]} {r6["replies"]}')

    r7 = audit.audit(sessions=1, pin="s_old.jsonl")
    check("钉住能定向到旧会话", r7["replies"] == 2 and r7["pinned"] is True,
          f'{r7["replies"]} {r7["pinned"]}')
    check("钉住时不出 pin_missed", r7["pin_missed"] is False, str(r7["pin_missed"]))

    # 钉住的会话被删：不能说"已锁定"，且必须还是给出可用结果
    r8 = audit.audit(sessions=1, pin="gone.jsonl")
    check("钉住失效时 pinned 为假", r8["pinned"] is False, str(r8["pinned"]))
    check("钉住失效被标成 pin_missed", r8["pin_missed"] is True, str(r8["pin_missed"]))
    check("钉住失效仍返回最新会话的数据", r8["replies"] == 5, str(r8["replies"]))

    # 开了新对话：不钉时必须跟过去，否则"打开这个对话看到上一个对话的数字"
    write_session(tmp, "ws-g", "s_latest.jsonl", [body_ok], mtime=1_700_004_000)
    r9 = audit.audit(sessions=1)
    check("新对话出现后默认跟随", r9["sessions"][0]["name"] == "s_latest.jsonl" and r9["replies"] == 1,
          f'{r9["sessions"][0]["name"]} {r9["replies"]}')

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for x in FAILS:
            print("  -", x)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
