"""注入生效自检 —— 回答「这一轮，宿主到底读到了什么」。

为什么需要它：磁盘上写成功 ≠ 宿主读到了。这一步以前只能靠用户自己看行为变化，
于是「注入没生效」反复被误判成「工具没写进去」。现在直接从宿主自己的会话记录里读回来。

数据从哪来（2026-10-30 实测，不是推断）：
    ~/.workbuddy/projects/<workspace-slug>/<sessionId>.jsonl
    每行一条记录，`type == "message"` 的记录里 role 与 content 在**顶层**
    （不在 message 字段里，第一次按 message.role 找是 0 命中）。
    宿主注入的 <identity_context> 只出现在 `role == "user"` 的消息里。

为什么只认 role=user：assistant 消息里也会出现同一串标记——那是模型在回复里
复述/引用它（本会话里就出现过）。按字符串找会把"模型引用"当成"宿主注入"。
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PROJECTS_DIR = Path.home() / ".workbuddy" / "projects"

# 宿主注入段的分隔标记（实测原样）
ID_OPEN = "<identity_context>"
ID_CLOSE = "</identity_context>"

BLOCK_RE = re.compile(r"persona-hub:begin\s+id=([\w-]+)\s+hash=([0-9a-f]+)")
# 区块的结束标记。没有 identity_context 闭合标签时靠它收口，防止把整条消息当注入段。
BLOCK_END_RE = re.compile(r"persona-hub:end")


def workspace_slug(cwd: str | Path) -> str:
    """把工作区路径折成宿主用的目录名。

    实测：`./`
        → `session-store`
    做法是小写化后把所有分隔符（: \\ / .）换成 '-'。
    不用"取最新目录"代替：同一个工作区可能同时开多个会话目录，取最新会串到别人的。
    """
    s = str(cwd).replace("\\", "/").lower()
    s = re.sub(r"[:/.]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s


@dataclass
class Turn:
    """一条带注入内容的用户消息。"""

    path: Path
    ts: float
    chars: int
    segment: str            # <identity_context> … </identity_context> 原文
    personas: list[dict] = field(default_factory=list)  # [{id, hash}]
    truncated: bool = False  # 没有闭合标签、按窗口截取过（结论要带这个前提）


def _read_records(path: Path, tail_bytes: int = 0):
    """逐行读 jsonl。tail_bytes > 0 时只读文件尾部。

    为什么要读尾部：单个会话文件实测能到 22 MB，而自检只关心"最近一轮"。
    从后往前读 3 MB 足够覆盖最后几轮，耗时从数百毫秒降到十几毫秒。
    按字节切会导致首行可能被切断，所以跳过第一条不完整的行。
    """
    try:
        with io.open(path, "rb") as fh:
            if tail_bytes:
                fh.seek(0, io.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - tail_bytes))
                raw = fh.read()
                chunks = raw.decode("utf-8", "replace").splitlines()
                if size > tail_bytes and chunks:
                    chunks = chunks[1:]      # 第一行大概率被切断
            else:
                chunks = io.open(path, encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        return
    for ln in chunks:
        ln = ln.strip()
        if not ln:
            continue
        try:
            yield json.loads(ln)
        except Exception:  # noqa: BLE001
            continue


def _message_text(rec: dict) -> str:
    c = rec.get("content")
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return ""
    out = []
    for b in c:
        if isinstance(b, dict):
            t = b.get("text") or b.get("content")
            if isinstance(t, str):
                out.append(t)
    return "\n".join(out)


def latest_injection(cwd: str | Path | None = None) -> Turn | None:
    """找最近一条宿主注入了身份文件的消息；没有则 None。

    cwd 为 None 时**全局找最新的**：用户真正关心的是"我这一轮读到了吗"，
    而工作区路径我们不一定知道（宿主的会话工作区可以任意指定）。
    """
    if cwd is None:
        files = sorted(
            PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:6]
    else:
        slug = workspace_slug(cwd)
        base = PROJECTS_DIR / slug
        if base.is_dir():
            files = sorted(
                base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
            )[:4]
        else:
            files = sorted(
                PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
            )[:6]

    best: Turn | None = None
    for f in files:
        for rec in _read_records(f, tail_bytes=3_000_000):
            if rec.get("type") != "message" or rec.get("role") != "user":
                continue
            text = _message_text(rec)
            # 只认**消息开头**的 identity_context。
            #
            # 这是唯一可靠的判据：宿主把身份前言拼在用户提问之前，所以它一定在开头。
            # 窗口取下 400 字符，依据是实测：本机 13 次真实注入的起点全在 252–269 之间
            # （前面是 <system-reminder> 之类的包裹标签），而正文里引用到的那些永远更靠后。
            # 踩过的坑（实测，长会话里必然发生）：会话被压缩后的续写消息会把
            # 我自己的历史输出整段塞进 user 消息，里面就含 `<identity_context>`
            # 这段字面量；按全消息搜索会把它当注入段，还会顺着找到
            # `id=keepme hash=aaaa1111`（我自己测试脚本的原文），
            # 结论变成"上下文里多了一个不存在的人格"——纯属自己吓自己。
            head = text[:400]
            i = head.find(ID_OPEN)
            if i < 0:
                continue
            j = text.find(ID_CLOSE, i)
            truncated = False
            if j > 0:
                seg = text[i : j + len(ID_CLOSE)]
            else:
                # 没有闭合标签时**不能**取到消息末尾，也不能只靠 persona-hub:end 收口。
                #
                # 实测两次踩坑：
                #   ① 取到消息末尾 → 整条 27 万字符的消息都算注入段，
                #      里面的字符串 `id=keepme hash=aaaa1111`（我自己脚本的原文）
                #      被当成人格块，误报"上下文里有两个身份"。
                #   ② 改成取到最后一个 persona-hub:end → 还是跨到了脚本里，
                #      因为脚本里也写着 persona-hub:end。
                #
                # 可靠的判据是长度：宿主这段身份前言实测约 3 千字符，
                # 所以无闭合标签时只取前 8000 字符，并标记 truncated 供上层说明。
                seg = text[i : i + 8000]
                truncated = True
            ts = float(rec.get("timestamp") or 0) / 1000.0
            personas = [{"id": m.group(1), "hash": m.group(2)} for m in BLOCK_RE.finditer(seg)]
            cur = Turn(path=f, ts=ts, chars=len(seg), segment=seg, personas=personas, truncated=truncated)
            if best is None or cur.ts > best.ts:
                best = cur
    return best


def disk_personas(files: list[Path]) -> list[dict]:
    """磁盘上当前挂着的人格块（id + hash）。"""
    out: list[dict] = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in BLOCK_RE.finditer(text):
            out.append({"id": m.group(1), "hash": m.group(2), "file": p.name})
    return out


def latest_user_ts(cwd: str | Path | None = None) -> float:
    """最近一条用户消息的时间。用来判断"最新一轮有没有注入"。"""
    if cwd is None:
        files = sorted(
            PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:6]
    else:
        base = PROJECTS_DIR / workspace_slug(cwd)
        files = (
            sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:4]
            if base.is_dir()
            else []
        )
    best = 0.0
    for f in files:
        for rec in _read_records(f, tail_bytes=1_000_000):
            if rec.get("type") == "message" and rec.get("role") == "user":
                ts = float(rec.get("timestamp") or 0) / 1000.0
                best = max(best, ts)
    return best


def count_turns_after(session: Path, ts: float) -> int:
    """同一条会话里，指定时间点之后又过了几个用户轮次。

    为什么限定"同一条会话"：全局数会把同时开着的别的会话算进来，
    得到的数字看起来合理、实际是错的。而"人格还能撑多久"这个问题只在单条会话内有意义。
    """
    n = 0
    for rec in _read_records(session, tail_bytes=6_000_000):
        if rec.get("type") == "message" and rec.get("role") == "user":
            if float(rec.get("timestamp") or 0) / 1000.0 > ts:
                n += 1
    return n


def verify(cwd: str | Path | None, files: list[Path], window: int = 10000) -> dict:
    """给出一个能被用户直接读的判断，而不是一堆原始数据。

    五种结论，覆盖所有实际会遇到的失败形态：
      no_session   —— 还没会话记录，没法判
      no_injection —— 有会话，但宿主没注入身份文件（通道选错/宿主不读）
      ok           —— 上下文里的人格与磁盘一致
      stale        —— 上下文里有，但 hash 和磁盘不同（读到了旧版本）
      missing      —— 磁盘上有，上下文里没有（写了但没进去）
    """
    turn = latest_injection(cwd)
    disk = disk_personas(files)

    if turn is None:
        return {
            "verdict": "no_session",
            "summary": "还没有可用的会话记录，无法自检。先在客户端里发一句话，再回来点一次。",
            "in_context": [],
            "on_disk": disk,
            "window": window,
        }

    ctx = turn.personas
    disk_keys = {(d["id"], d["hash"]) for d in disk}
    ctx_keys = {(d["id"], d["hash"]) for d in ctx}

    # 最新一轮有没有注入：注入段可能是好几轮之前的，直接把旧的当"当前生效"会骗人
    last_msg = latest_user_ts(cwd)
    gap = last_msg - turn.ts
    stale_turn = gap > 5  # 秒；同一轮内的时间差是毫秒级

    if stale_turn:
        # 实测（2026-10-30）：同一条会话 116 个用户消息里只有 7 轮带注入，
        # 且都紧跟 USER.md 被改动之后——**宿主不是每轮都注入**。
        # 但注入过的内容会留在对话历史里，所以"本轮没注入"通常≠"人格丢了"。
        # 这两件事必须分开说，否则会让用户以为又坏了。
        mins = gap / 60
        ago = f"{mins:.0f} 分钟前" if mins >= 1 else f"{gap:.0f} 秒前"
        since = count_turns_after(turn.path, turn.ts)
        verdict = "carried"
        summary = (
            f"**本轮没有重新注入**（上次注入是 {ago}，中间又走了 {since} 轮）。这不等于失效——"
            f"注入过的内容还留在同一会话的对话历史里，通常仍然生效；"
            f"轮次越多，它被上下文压缩挤掉的可能性越大。"
            f"要确认最新版本已生效，开一个新会话再看一次。"
        )
    elif not ctx:
        verdict, summary = (
            "no_injection",
            "宿主这一轮的上下文里**没有**身份文件的内容——说明这条通道没被读取。"
            "检查是不是写错了通道（专家会话只读 USER.md），或客户端还没重开。",
        )
    elif ctx_keys & disk_keys and ctx_keys <= disk_keys:
        verdict, summary = "ok", "上下文里的人格与磁盘一致，这一轮确实读到了。"
    elif ctx_keys & disk_keys:
        verdict, summary = (
            "partial",
            f"上下文里有 {len(ctx)} 个、磁盘上有 {len(disk)} 个，只有部分对得上——"
            "通常是写入了新的人格但宿主读的还是旧版本。",
        )
    elif ctx and not (ctx_keys & disk_keys):
        verdict, summary = (
            "stale",
            "上下文里有身份块，但 hash 和磁盘上的都不一样——宿主读到的是**旧版本**，"
            "需要完全退出客户端重开。",
        )
    else:
        verdict, summary = (
            "missing",
            "磁盘上有人格块，但宿主这一轮的上下文里没有——写了但没进去。",
        )

    return {
        "verdict": verdict,
        "summary": summary,
        "session": str(turn.path),
        "session_ts": turn.ts,
        "turns_since": count_turns_after(turn.path, turn.ts),
        "segment_chars": turn.chars,
        "window": window,
        "truncated": turn.chars > window,
        "in_context": ctx,
        "on_disk": disk,
    }
