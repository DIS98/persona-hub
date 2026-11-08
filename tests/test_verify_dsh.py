"""DSH 注入自检的判据测试。

为什么要造多帧文件
==================
DSH 每轮 append 一帧 zstd，一个 51 KB 的会话文件实测有 10 帧。
`ZstdDecompressor().decompress()` 只吐第一帧（244 字节的会话头），
照那个实现写出来的自检会永远报「没有注入」——这就是最典型的静默失效。

所以每个用例都真造两帧：第一帧只有会话头，第二帧才有 agent-instructions。
判据对了才可能通过；判据退化成"解第一帧"就会红。

全部在临时目录里跑，不碰 ~/.dsh。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zstandard  # noqa: E402

from core import verify_dsh  # noqa: E402

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


def frame(records: list[dict]) -> bytes:
    """一帧 = 一次 append。DSH 就是这么写的。"""
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
    return zstandard.ZstdCompressor().compress(body.encode("utf-8"))


def write_session(root: Path, cwd: str, sid: str, instr_text: str | None) -> Path:
    slug = verify_dsh.workspace_slug(cwd)
    d = root / slug / sid
    d.mkdir(parents=True, exist_ok=True)
    p = d / "session.v4.jsonl.zstd"

    head = [{"type": "session", "version": 4, "id": sid, "cwd": cwd}]
    if instr_text is None:
        tail = [{"type": "user/message", "time": 2000, "data": {
            "source": {"kind": "user"}, "content": [{"type": "text", "text": "你好"}]}}]
    else:
        tail = [
            {"type": "user/message", "time": 2000, "data": {
                "source": {"kind": "user"}, "content": [{"type": "text", "text": "你好"}]}},
            {"type": "user/message", "time": 2001, "data": {
                "source": {"kind": "agent-instructions", "form": "instructions"},
                "content": [{"type": "text", "text": instr_text}]}},
            # 同一轮的其它注入：不能被当成人格通道
            {"type": "user/message", "time": 2002, "data": {
                "source": {"kind": "runtime-context", "form": "snapshot"},
                "content": [{"type": "text", "text": "Current working directory: ..."}]}},
        ]

    p.write_bytes(frame(head) + frame(tail))
    return p


def agents_md(body: str) -> str:
    return (
        "# 工作约定\n\n"
        "## 1. 环境优先\n\n先搜本机已有什么。\n\n"
        f"<!-- persona-hub:begin id=imported hash=c9b616b8 -->\n{body}\n"
        "<!-- persona-hub:end -->\n"
    )


def instr_from(md_text: str, persona_body: str, hash_override: str | None = None) -> str:
    """模拟 DSH 拼出来的那条 agent-instructions 消息。

    结构照抄实测原文：system-reminder 包裹 + `Instructions from: ~/.dsh/AGENTS.md`。
    hash_override 用来造"读到旧版本"的变异。
    """
    md = md_text.replace("hash=c9b616b8", f"hash={hash_override}") if hash_override else md_text
    return (
        "<system-reminder>\n"
        "The following workspace instructions may be relevant to your work.\n\n"
        f"Instructions from: ~/.dsh/AGENTS.md\n\n{md}\n"
        "</system-reminder>"
    )


BODY = "【身份】执行体，不是顾问。收到请求直接产出可用的东西。\n【完成判据】末行是 `▣ ARMOR #N`。"


def main() -> int:
    print("=" * 56)
    print("DSH 注入自检判据")
    print("=" * 56)

    tmp = Path(tempfile.mkdtemp(prefix="ph-dsh-"))
    try:
        real_sessions, real_md = verify_dsh.DSH_SESSIONS, verify_dsh.DSH_AGENTS_MD
        verify_dsh.DSH_SESSIONS = tmp / "sessions"
        verify_dsh.DSH_AGENTS_MD = tmp / "AGENTS.md"

        md = agents_md(BODY)
        verify_dsh.DSH_AGENTS_MD.write_text(md, encoding="utf-8")
        cwd = r"./"

        # ------------------------------------------------------- A slug
        print("\n[A] 会话目录名折法")
        check(
            "slug 与实测形态一致",
            verify_dsh.workspace_slug(cwd)
            == "--C-Users-x-Documents-deepseek-harness-default-workspace--",
            verify_dsh.workspace_slug(cwd),
        )

        # ------------------------------------------------------- B 无会话
        print("\n[B] 没有会话")
        r = verify_dsh.verify(cwd)
        check("no_session", r["verdict"] == "no_session", r["verdict"])

        # ------------------------------------------------------- C 正常
        print("\n[C] 多帧里第二帧才有人格")
        write_session(tmp / "sessions", cwd, "s1", instr_from(md, BODY))
        r = verify_dsh.verify(cwd)
        check("ok", r["verdict"] == "ok", r["verdict"])
        check("读到的是第二帧", r.get("segment_chars", 0) > 200, str(r.get("segment_chars")))
        check("人格 id/hash 正确", r["in_context"] == [{"id": "imported", "hash": "c9b616b8"}], str(r["in_context"]))
        check("来源是 AGENTS.md", "~/.dsh/AGENTS.md" in r.get("sources", []), str(r.get("sources")))
        check("摘要不含 runtime-context 误判", "runtime" not in str(r.get("in_context")), str(r.get("in_context")))

        # ------------------------------------------------------- D 变异：hash 不一致
        print("\n[D] 变异：注入里是旧 hash")
        shutil.rmtree(tmp / "sessions")
        write_session(tmp / "sessions", cwd, "s2", instr_from(md, BODY, hash_override="deadbeef"))
        r = verify_dsh.verify(cwd)
        check("stale", r["verdict"] == "stale", r["verdict"])

        # ------------------------------------------------------- E 变异：注入里没块
        print("\n[E] 变异：注入里没有人格块")
        shutil.rmtree(tmp / "sessions")
        write_session(tmp / "sessions", cwd, "s3", instr_from("", BODY).replace(
            "<!-- persona-hub:begin id=imported hash=c9b616b8 -->", ""
        ).replace("<!-- persona-hub:end -->", "").replace(BODY, ""))
        r = verify_dsh.verify(cwd)
        check("missing", r["verdict"] == "missing", r["verdict"])

        # ------------------------------------------------------- F 变异：没有指令链
        print("\n[F] 变异：插件被关掉，没有任何注入")
        shutil.rmtree(tmp / "sessions")
        write_session(tmp / "sessions", cwd, "s4", None)
        r = verify_dsh.verify(cwd)
        check("no_injection", r["verdict"] == "no_injection", r["verdict"])

        # ------------------------------------------------------- G 单帧就够吗
        print("\n[G] 只有第一帧时不能误判为 ok")
        shutil.rmtree(tmp / "sessions")
        d = tmp / "sessions" / verify_dsh.workspace_slug(cwd) / "s5"
        d.mkdir(parents=True)
        (d / "session.v4.jsonl.zstd").write_bytes(
            frame([{"type": "session", "version": 4, "id": "s5", "cwd": cwd}])
            + frame([{"type": "user/message", "time": 1, "data": {
                "source": {"kind": "agent-instructions"},
                "content": [{"type": "text", "text": instr_from(md, BODY)}]}}])
        )
        r = verify_dsh.verify(cwd)
        check("两帧都读到", r["verdict"] == "ok", r["verdict"])

        # ------------------------------------------------------- G2 关键变异
        # 把"多帧切分"退回成"只解第一帧"——这是最容易写出来、也最容易静默失效的实现。
        # 形态上它照样能跑出结论（还会返回一个 verdict），但结论永远是"没注入"。
        print("\n[G2] 变异：只解第一帧的实现必须被判死")
        orig_frames = verify_dsh._frames
        verify_dsh._frames = lambda buf: orig_frames(buf)[:1]
        try:
            r = verify_dsh.verify(cwd)
            check("不再是 ok", r["verdict"] != "ok", r["verdict"])
            check("给出的是没读到一类", r["verdict"] in {"missing", "partial", "no_injection"}, r["verdict"])
        finally:
            verify_dsh._frames = orig_frames

        # ------------------------------------------------------- H 缺模块时不崩
        print("\n[H] 缺 zstandard 的降级")
        shutil.rmtree(tmp / "sessions")
        write_session(tmp / "sessions", cwd, "s6", instr_from(md, BODY))
        saved = sys.modules.get("zstandard")
        sys.modules["zstandard"] = None  # type: ignore[assignment]
        try:
            r = verify_dsh.verify(cwd)
            check("降级成 no_session 而不是抛异常", r["verdict"] == "no_session", r["verdict"])
        finally:
            if saved is not None:
                sys.modules["zstandard"] = saved
            else:
                sys.modules.pop("zstandard", None)

        verify_dsh.DSH_SESSIONS, verify_dsh.DSH_AGENTS_MD = real_sessions, real_md
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"通过 {PASS} / 失败 {FAIL}")
    if FAIL:
        for f in FAILS:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
