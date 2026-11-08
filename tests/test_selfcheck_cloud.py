"""生效自检 + 云记忆的判据测试。

两个模块都在跟"宿主自己的东西"打交道，出错代价高（一个是误报生效，一个是弄坏账号记忆），
所以每条判据都要能抓错：给一个看起来对、实际不对的输入，看它是否报出正确结论。

全部在临时目录里跑，不碰本机真实的云记忆与会话记录。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import cloudmem, verify  # noqa: E402

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


# ---------------------------------------------------------------- 造数据
def write_session(root: Path, slug: str, records: list[dict]) -> Path:
    d = root / slug
    d.mkdir(parents=True, exist_ok=True)
    f = d / "s1.jsonl"
    with io.open(f, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return f


def user_msg(text: str, ts: int = 1_700_000_000_000) -> dict:
    """按实测 schema 造：role 与 content 在顶层，content 是块数组。"""
    return {
        "type": "message",
        "role": "user",
        "timestamp": ts,
        "content": [{"type": "input_text", "text": text}],
    }


def identity_text(persona_id: str, h: str) -> str:
    return (
        '<system-reminder data-role="user-context">\n'
        "<identity_context>\n## USER.md\n"
        f"<!-- persona-hub:begin id={persona_id} hash={h} -->\n正文\n<!-- persona-hub:end -->\n"
        "</identity_context>\n</system-reminder>"
    )


def disk_file(tmp: Path, persona_id: str, h: str) -> Path:
    p = tmp / "USER.md"
    p.write_text(
        f"# USER.md\n<!-- persona-hub:begin id={persona_id} hash={h} -->\n正文\n"
        f"<!-- persona-hub:end -->\n",
        encoding="utf-8",
    )
    return p


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ph-verify-"))
    # 把两个模块的根目录指到临时目录
    verify.PROJECTS_DIR = tmp / "projects"
    verify.PROJECTS_DIR.mkdir()

    print("\n[A] 宿主注入段解析（schema 必须是 role/content 在顶层）")
    f = write_session(verify.PROJECTS_DIR, "ws-a", [user_msg(identity_text("p1", "aaaa1111"))])
    turn = verify.latest_injection()
    check("能找出注入段", turn is not None, str(turn))
    check("解析出人格 id/hash",
          bool(turn) and turn.personas == [{"id": "p1", "hash": "aaaa1111"}],
          str(turn.personas if turn else None))

    print("\n[B] 只认 role=user（模型回复里引用同一串不能算注入）")
    f2 = write_session(
        verify.PROJECTS_DIR,
        "ws-b",
        [
            {"type": "message", "role": "assistant", "timestamp": 1_700_000_002_000,
             "content": [{"type": "text", "text": identity_text("fake", "deadbeef")}]},
        ],
    )
    turn2 = verify.latest_injection()
    check("assistant 里的标记不算注入",
          turn2 is None or all(p["id"] != "fake" for p in turn2.personas),
          str(turn2.personas if turn2 else None))

    print("\n[C] 四种判定都要能报出来")
    d1 = disk_file(tmp, "p1", "aaaa1111")
    r = verify.verify(None, [d1])
    check("一致 → ok", r["verdict"] == "ok", r["verdict"])

    d2 = disk_file(tmp, "p1", "99999999")   # 磁盘换了，上下文还是旧的
    r = verify.verify(None, [d2])
    check("hash 不同 → stale", r["verdict"] == "stale", r["verdict"])

    verify.PROJECTS_DIR2 = None
    # 造一个"没有注入"的会话：把它放进一个更新的文件里，压过前面的
    write_session(
        verify.PROJECTS_DIR,
        "ws-c",
        [user_msg("普通用户消息，没有 identity_context", ts=1_800_000_000_000)],
    )
    r = verify.verify(None, [d1])
    # 注入段是好几轮之前的 → 不能说"已生效"，但也不能说"失效"：
    # 实测宿主不是每轮注入，注入过的内容会留在历史里。所以要单独报 carried。
    check("最新一轮没注入 → carried（不是失效）", r["verdict"] == "carried", r["verdict"])
    check("说明里点出上次注入是什么时候", "上次注入" in r["summary"], r["summary"][:60])
    check("说明里点明这不等于失效", "不等于失效" in r["summary"], r["summary"][:80])

    print("\n[C2] 轮次间隔（用来量化「人格还能撑多久」）")
    # 造：注入在 t=1000，之后又有 3 条用户消息
    ts0 = 1_700_000_000_000
    f_d = write_session(
        verify.PROJECTS_DIR,
        "ws-d",
        [user_msg(identity_text("p1", "aaaa1111"), ts=ts0)]
        + [user_msg("后续普通消息", ts=ts0 + 1000 * (i + 1)) for i in range(3)],
    )
    got = verify.count_turns_after(f_d, ts0 / 1000.0)
    check("数得对：注入之后还有 3 条用户消息", got == 3, str(got))
    check("只看本条会话（别的会话不混进来）",
          verify.count_turns_after(f_d, (ts0 / 1000.0) + 10_000) == 0, "应为 0")

    print("\n[C3] 消息正文里引用到的 identity_context 不算注入（实测踩过的假警报）")
    # 会话被压缩后的续写消息会把往期对话整段塞进 user 消息，里面含
    # `<identity_context>` 与 `persona-hub:begin` 这些字面量。按全消息搜索就会
    # 把它们当成宿主的注入段，报出磁盘上并不存在的人格（实测报出了 id=keepme）。
    # 判据：注入前言只认消息开头，后面的引用一律不认。
    quoted = (
        "以下是上一轮的命令原文，仅供参考：\n" + ("填充内容。" * 300) + "\n"
        '<identity_context>\n<!-- persona-hub:begin id=keepme hash=aaaa1111 -->\n'
        "正文\n<!-- persona-hub:end -->\n</identity_context>\n"
    )
    f_q = write_session(
        verify.PROJECTS_DIR, "ws-quoted", [user_msg(quoted, ts=1_900_000_000_000)]
    )
    turn = verify.latest_injection()
    ids = [p["id"] for p in (turn.personas if turn else [])]
    check("正文里引用的块不算注入", "keepme" not in ids, f"解析到 {ids}")
    # 注意断言的对象是"这一条消息"，不是"全局找不到注入"：
    # 全局扫描会命中别的工作区的正常注入，那样断言会误伤。
    check(
        "该消息不被当成注入消息",
        turn is None or turn.path != f_q,
        f"命中 {getattr(turn, 'path', None)}",
    )

    # 反过来：真的放在开头的注入，必须照旧认得出来（别为了防误报把正路堵死）
    f_ok = write_session(
        verify.PROJECTS_DIR, "ws-head", [user_msg(identity_text("p9", "bbbb2222"), ts=1_900_000_100_000)]
    )
    turn = verify.latest_injection()
    ids = [p["id"] for p in (turn.personas if turn else [])]
    check("开头的注入仍能识别", ids == ["p9"], str(ids))

    print("\n[D] 云记忆：读 / 写 / 备份 / 防覆盖 / 还原")
    mem = tmp / "memory"
    mem.mkdir()
    cloudmem.MEMORY_DIR = mem
    p = mem / "u1_memory.md"
    p.write_text(
        "# User Memory Profile\n"
        "> Last updated: 2026-01-01T00:00:00.000Z\n"
        "> Version: 7\n\n"
        "## Memory Block\n\n\n---\n\n"
        "<!-- RAW_JSON_START\n"
        + json.dumps({"uid": "u1", "memoryBlock": "", "updatedAt": "x"}, ensure_ascii=False, indent=2)
        + "\nRAW_JSON_END -->\n",
        encoding="utf-8",
    )
    m = cloudmem.read()
    check("能读到文件", m is not None and m.version == 7, str(m))
    check("空正文时双写一致", bool(m) and m.raw_ok, str(m))

    res = cloudmem.write("第一版内容")
    check("写入成功", res.get("ok") is True, str(res))
    check("版本递增（不当递增会被宿主旧值覆盖）", cloudmem.read().version == 8, str(cloudmem.read().version))
    check("正文回读一致", cloudmem.read().block == "第一版内容", cloudmem.read().block)
    check("RAW_JSON 与正文一致", cloudmem.read().raw_ok, "不一致")
    check("写入前留了备份", bool(res.get("backup")) and Path(res["backup"]).is_file(), str(res.get("backup")))
    check("备份内容是写入前的（空正文）", cloudmem.read.__module__ and "## Memory Block" in Path(res["backup"]).read_text(encoding="utf-8"))

    # 关键护栏：正文非空且未加 force → 必须拒绝
    res2 = cloudmem.write("第二版", force=False)
    check("非空时不加 force 拒绝覆盖", res2.get("ok") is False and res2.get("error") == "OCCUPIED", str(res2))
    check("被拒绝后正文没变", cloudmem.read().block == "第一版内容", cloudmem.read().block)

    bak = res["backup"]
    res3 = cloudmem.write("第二版", force=True)
    check("force 后可以写", res3.get("ok") is True, str(res3))

    rr = cloudmem.restore(bak)
    check("能从备份还原", rr.get("ok") is True, str(rr))
    check("还原后正文是空的（备份时点）", cloudmem.read().block == "", repr(cloudmem.read().block))

    # 越权还原：路径不在 memory 目录内必须拒绝
    outside = tmp / "outside.bak"
    outside.write_text("x", encoding="utf-8")
    bad = cloudmem.restore(str(outside))
    check("拒绝目录外的路径", bad.get("ok") is False and bad.get("error") == "FORBIDDEN", str(bad))

    print("\n[E] 超窗要告警")
    big = "字" * (cloudmem.MEMORY_BLOCK_LIMIT + 100)
    res4 = cloudmem.write(big, force=True)
    check("超窗仍然写入但给告警", res4.get("ok") is True and any("超" in w for w in res4.get("warnings", [])), str(res4.get("warnings")))

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for x in FAILS:
            print("  -", x)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
