"""DSH（DeepSeek Harness）注入自检 —— 回答「DSH 这一轮到底读到了什么」。

为什么单开一个模块而不是复用 `verify.py`
=========================================
`verify.py` 判的是 WorkBuddy 的会话记录：`~/.workbuddy/projects/<slug>/<id>.jsonl`，
注入载体是 `role == "user"` 消息开头的 `<identity_context>`。

DSH 三处都不同，套不进去：

| 项 | WorkBuddy | DSH |
|---|---|---|
| 位置 | `~/.workbuddy/projects/*/*.jsonl` | `~/.dsh/sessions/<slug>/<sid>/session.v4.jsonl.zstd` |
| 压缩 | 明文 jsonl | **多帧 zstd**（append 写入，一个文件几十帧） |
| 载体 | `<identity_context>` 标签 | `data.source.kind == "agent-instructions"` 的 user 消息 |

实测依据（2026-11-05，本机）
----------------------------
* `session.v4.jsonl.zstd` 用 `ZstdDecompressor().decompress()` 只解出 244 字节（第一帧，
  仅会话头）。按魔数 `28 b5 2f fd` 切帧后逐帧解压，同一文件得 10 帧 / 140 KB。
* 指令链不是 system 消息。`system/message` 里只有项目自带的 `personaPrefix`
  （cordis.patch.yml 注入的那段），AGENTS.md 完全不在里面。
* 真正的位置是一条 `user/message`，`source = {kind: "agent-instructions", form:
  "instructions"}`，正文以 `<system-reminder>` 开头，内含
  `Instructions from: ~/.dsh/AGENTS.md`。
  用 source.kind 判定，比字符串匹配稳——那个标签用户在提问里也能打出来。
* 同一轮还会有 `runtime-context` / `skill-catalog` / `time-context` 三条注入，
  它们也是 user 消息，**不能**当成"注入了身份"。
* `~/.dsh/managed-prompts/` 在 DSH 安装目录里搜不到任何引用（全量 grep 0 命中），
  说明它不是 DSH 的读取路径。人格要求写进 `AGENTS.md` 才生效。
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .paths import HOME

DSH_SESSIONS = HOME / ".dsh" / "sessions"
DSH_AGENTS_MD = HOME / ".dsh" / "AGENTS.md"

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

BLOCK_RE = re.compile(r"persona-hub:begin\s+id=([\w-]+)\s+hash=([0-9a-f]+)")
SOURCE_RE = re.compile(r"Instructions from:\s*(.+)")


def _frames(buf: bytes) -> list[bytes]:
    """按魔数切帧。

    为什么不信"整份直接解"：DSH 每轮 append 一帧，解压器默认只吐第一帧，
    拿到的永远是会话刚创建时那 244 字节的头部，看起来像"没有任何注入"。
    """
    starts = []
    i = 0
    while True:
        i = buf.find(ZSTD_MAGIC, i)
        if i < 0:
            break
        starts.append(i)
        i += 4
    out = []
    for k, a in enumerate(starts):
        b = starts[k + 1] if k + 1 < len(starts) else len(buf)
        out.append(buf[a:b])
    return out


def read_records(path: Path, tail_bytes: int = 8_000_000) -> list[dict]:
    """读一个会话文件的全部记录。缺 zstandard 时返回空列表（上层判 unsupported）。"""
    try:
        import zstandard  # noqa: PLC0415
    except ImportError:
        raise RuntimeError("missing-zstandard") from None

    try:
        with io.open(path, "rb") as fh:
            fh.seek(0, io.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            buf = fh.read()
    except OSError:
        return []

    dctx = zstandard.ZstdDecompressor()
    chunks = []
    for fr in _frames(buf):
        try:
            chunks.append(dctx.decompress(fr, max_output_size=64 * 1024 * 1024))
        except Exception:  # noqa: BLE001
            continue   # 单帧坏了不影响其它帧；丢一帧最多丢一轮
    text = b"".join(chunks).decode("utf-8", "replace")

    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:  # noqa: BLE001
            continue
    return out


def workspace_slug(cwd: str | Path) -> str:
    """`./`
    → `--C-Users-x-Documents-deepseek-harness-default-workspace--`

    实测形态：首尾各两个连字符，中间的分隔符全换成 '-'，字母大小写保留。
    注意与 WorkBuddy 的 slug **不一样**（那边是小写且不带首尾横线），不能共用一个函数。
    """
    s = re.sub(r"[\\/:]+", "-", str(cwd))
    return f"--{s}--"


@dataclass
class Injection:
    """一轮真实的指令链注入。"""

    path: Path
    session: str
    ts: float
    chars: int
    text: str
    sources: list[str] = field(default_factory=list)
    personas: list[dict] = field(default_factory=list)


def _msg_text(rec: dict) -> str:
    d = rec.get("data") or {}
    content = d.get("content")
    if not isinstance(content, list):
        return ""
    parts = []
    for b in content:
        if isinstance(b, dict) and isinstance(b.get("text"), str):
            parts.append(b["text"])
    return "\n".join(parts)


def _is_instructions(rec: dict) -> bool:
    d = rec.get("data") or {}
    src = d.get("source") or {}
    return rec.get("type") == "user/message" and src.get("kind") == "agent-instructions"


def _sessions_newest_first(cwd: str | Path | None) -> list[Path]:
    if not DSH_SESSIONS.is_dir():
        return []
    if cwd is not None:
        slug = workspace_slug(cwd)
        base = DSH_SESSIONS / slug
        roots = [base] if base.is_dir() else [DSH_SESSIONS]
    else:
        roots = [DSH_SESSIONS]

    files: list[Path] = []
    for r in roots:
        files.extend(r.glob("*/session.v4.jsonl.zstd"))
        files.extend(r.glob("*/*/session.v4.jsonl.zstd"))
    # 去重（两种 glob 可能命中同一个文件）
    seen = set()
    uniq = []
    for f in files:
        k = str(f.resolve())
        if k in seen:
            continue
        seen.add(k)
        uniq.append(f)
    return sorted(uniq, key=lambda p: p.stat().st_mtime, reverse=True)


def latest_injection(cwd: str | Path | None = None, files_limit: int = 6) -> Injection | None:
    """最近一条 agent-instructions 注入。

    取"最近的会话文件"，在它里面取最后一条 agent-instructions：
    DSH 每个 turn 都会重发一次指令链（`systemPromptUpdate: in-history`），
    所以最后一条就是这一轮模型实际看到的版本。
    """
    best: Injection | None = None
    for f in _sessions_newest_first(cwd)[:files_limit]:
        recs = read_records(f)
        for rec in recs:
            if not _is_instructions(rec):
                continue
            text = _msg_text(rec)
            ts = float(rec.get("time") or 0) / 1000.0
            cur = Injection(
                path=f,
                session=f.parent.name,
                ts=ts,
                chars=len(text),
                text=text,
                sources=[m.group(1).strip() for m in SOURCE_RE.finditer(text)],
                personas=[
                    {"id": m.group(1), "hash": m.group(2)}
                    for m in BLOCK_RE.finditer(text)
                ],
            )
            if best is None or cur.ts > best.ts:
                best = cur
    return best


def disk_personas(path: Path = DSH_AGENTS_MD) -> list[dict]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [
        {"id": m.group(1), "hash": m.group(2), "file": path.name}
        for m in BLOCK_RE.finditer(text)
    ]


def verify(cwd: str | Path | None = None) -> dict:
    """给出一句能直接读的结论。

    六种判定，全部落在界面已有的样式表内（多一个键前端就显示成"无法判定"）：
      no_session   —— 没有会话文件，或读不了压缩（缺 zstandard）
      no_injection —— 有会话，但没有 agent-instructions 注入（插件被禁用 / profile 关了）
      ok           —— 注入里的 hash 与 AGENTS.md 一致
      stale        —— 注入里有块，但是旧版本（改了文件没重开 DSH）
      partial      —— 注入里有块、AGENTS.md 里没有（人格来自别处写入）
      missing      —— AGENTS.md 有块，注入里没有
    """
    disk = disk_personas()

    try:
        inj = latest_injection(cwd)
    except RuntimeError as exc:
        if "missing-zstandard" in str(exc):
            return {
                "verdict": "no_session",
                "host": "dsh",
                "summary": (
                    "暂时读不了 DSH 的会话记录：缺 zstandard 模块。"
                    "装上它才能解压取证（pip install zstandard），装完再点一次。"
                ),
                "in_context": [],
                "on_disk": disk,
            }
        raise

    sessions = _sessions_newest_first(cwd)
    if not sessions:
        return {
            "verdict": "no_session",
            "host": "dsh",
            "summary": f"没找到 DSH 会话记录（{DSH_SESSIONS}）。先在 DSH 里发一句话，再回来点一次。",
            "in_context": [],
            "on_disk": disk,
        }

    if inj is None:
        return {
            "verdict": "no_injection",
            "host": "dsh",
            "summary": (
                "会话里没有任何指令链注入（agent-instructions 消息 0 条）——"
                "说明 DSH 的 dsh-agent-instructions 插件被关掉了，"
                "AGENTS.md 写得再对也不会被读。"
            ),
            "in_context": [],
            "on_disk": disk,
            "session": str(sessions[0]),
        }

    ctx_keys = {(p["id"], p["hash"]) for p in inj.personas}
    disk_keys = {(d["id"], d["hash"]) for d in disk}

    if not disk and not inj.personas:
        verdict, summary = (
            "missing",
            "AGENTS.md 和注入里都没有人格块——还没注入过。",
        )
    elif ctx_keys and ctx_keys & disk_keys:
        names = "、".join(f'{p["id"]}/{p["hash"]}' for p in inj.personas)
        where = inj.sources[0] if inj.sources else "AGENTS.md"
        verdict, summary = (
            "ok",
            f"DSH 这一轮确实读到了：{where} 里的人格块（{names}）"
            f"与磁盘一致，注入段共 {inj.chars} 字符。",
        )
    elif ctx_keys and not (ctx_keys & disk_keys):
        verdict, summary = (
            "stale",
            "注入里有人格块，但 hash 和磁盘上的不一样——DSH 读到的是旧版本。"
            "完全退出 DSH 再打开，或开一个新会话。",
        )
    elif inj.personas and not disk:
        verdict, summary = (
            "partial",
            "注入里有人格块，AGENTS.md 里却没有——人格来自别处写入，下次点「应用」会覆盖。",
        )
    else:
        verdict, summary = (
            "missing",
            "AGENTS.md 里有人格块，但这一轮的注入里没有——写了但没进去。"
            "检查指令链的字节预算（默认 65536）是否被更大的文件挤掉。",
        )

    return {
        "verdict": verdict,
        "summary": summary,
        "host": "dsh",
        "session": str(inj.path),
        "session_id": inj.session,
        "session_ts": inj.ts,
        "segment_chars": inj.chars,
        "sources": inj.sources,
        "in_context": inj.personas,
        "on_disk": disk,
        "sessions_scanned": len(sessions),
    }
