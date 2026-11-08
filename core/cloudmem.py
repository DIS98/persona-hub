"""账号级云记忆（memoryBlock）—— 读取、写入、备份、还原。

为什么单独一个模块：这是**宿主自己维护**的文件，不是我们的注入目标。
写坏了不是"人格没生效"，而是整个账号的记忆可能错乱，所以读写都得带全套护栏：
先备份、写完立刻回读校验、随时能还原。

文件（2026-10-30 实测）：
    ~/.workbuddy/memory/<uid>_memory.md

结构：
    # User Memory Profile
    > Last updated: <ISO>
    > Version: <n>

    ## Memory Block

    <正文>

    ---

    <!-- RAW_JSON_START
    {"uid": "...", "memoryBlock": "<正文>", "updatedAt": "<ISO>"}
    RAW_JSON_END -->

两个字段必须同时写：
- `## Memory Block` 人读
- RAW_JSON 里的 `memoryBlock` 是宿主真正同步/注入的那份
只改其中一个，会出现"看起来写了、注入还是旧的"。

`Version` 每次递增：宿主按它判断谁更新，不递增的话我们的写入会被它用旧值覆盖回去
（破甲一键通把这里直接写成 999999 就是为了永不被覆盖）。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

MEMORY_DIR = Path.home() / ".workbuddy" / "memory"

HEADER_RE = re.compile(r"^(# User Memory Profile\s*\n)(> Last updated:.*\n)(> Version:\s*)(\d+)", re.M)
BLOCK_RE = re.compile(r"(## Memory Block\s*\n)(.*?)(\n---\s*\n)", re.S)
RAW_RE = re.compile(r"(<!-- RAW_JSON_START\s*\n)(.*?)(\nRAW_JSON_END -->)", re.S)

# 云记忆的注入窗口（与 USER.md 的 10,000 同数量级，来自逆向对照，非本机实测）
MEMORY_BLOCK_LIMIT = 10000


@dataclass
class CloudMemory:
    path: Path
    version: int
    updated: str
    block: str          # 正文（两侧空白已去掉）
    raw_ok: bool        # RAW_JSON 里的 memoryBlock 与正文是否一致

    @property
    def chars(self) -> int:
        return len(self.block)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "version": self.version,
            "updated": self.updated,
            "block": self.block,
            "chars": self.chars,
            "limit": MEMORY_BLOCK_LIMIT,
            "raw_ok": self.raw_ok,
        }


def memory_file() -> Path | None:
    """找当前账号的云记忆文件。`.bak` 之类同名的备份不算。"""
    if not MEMORY_DIR.is_dir():
        return None
    files = [
        p
        for p in MEMORY_DIR.glob("*_memory.md")
        if ".bak" not in p.name and ".tmp" not in p.name
    ]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


def read() -> CloudMemory | None:
    p = memory_file()
    if p is None:
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    block = ""
    m = BLOCK_RE.search(text)
    if m:
        block = m.group(2).strip()

    version = 0
    updated = ""
    h = HEADER_RE.search(text)
    if h:
        updated = h.group(2).split(":", 1)[-1].strip()
        version = int(h.group(4))

    raw_ok = True
    r = RAW_RE.search(text)
    if r:
        try:
            data = json.loads(r.group(2))
            raw_ok = str(data.get("memoryBlock") or "").strip() == block
        except Exception:  # noqa: BLE001
            raw_ok = False

    return CloudMemory(path=p, version=version, updated=updated, block=block, raw_ok=raw_ok)


def backup(tag: str = "cloudmem") -> Path | None:
    """写之前先存一份。返回备份路径。

    文件名必须唯一：只用秒级时间戳时，同一秒内连续两次写入会**覆盖掉前一份备份**，
    于是"还原到写入前"实际还原到的是中间态。这是测试里踩出来的真 bug，不是理论风险。
    解法是加进程内自增序号，并在落盘前确保名字未被占用。
    """
    src = memory_file()
    if src is None:
        return None

    stamp = time.strftime("%Y%m%d-%H%M%S")
    for seq in range(1000):
        suffix = f"{stamp}-{seq}" if seq else stamp
        dst = src.with_name(f"{src.name}.{tag}-{suffix}.bak")
        if dst.exists():
            continue
        try:
            shutil.copy2(src, dst)
            return dst
        except OSError:
            return None
    return None


def _compose(old_text: str, block: str, bump: bool) -> str:
    """把新的正文塞回原文件结构，保留其余所有内容。"""
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())

    text = old_text
    h = HEADER_RE.search(text)
    if h:
        ver = int(h.group(4)) + 1 if bump else int(h.group(4))
        text = HEADER_RE.sub(
            lambda m: f"{m.group(1)}> Last updated: {now}\n> Version: {ver}",
            text,
            count=1,
        )
    else:
        text = f"# User Memory Profile\n> Last updated: {now}\n> Version: 1\n\n" + text

    if BLOCK_RE.search(text):
        text = BLOCK_RE.sub(lambda m: f"{m.group(1)}\n{block}\n{m.group(3)}", text, count=1)
    else:
        text = text.rstrip() + f"\n\n## Memory Block\n\n{block}\n\n---\n"

    def _raw(m: re.Match) -> str:
        try:
            data = json.loads(m.group(2))
        except Exception:  # noqa: BLE001
            data = {}
        data["memoryBlock"] = block
        data["updatedAt"] = now
        return f"{m.group(1)}{json.dumps(data, ensure_ascii=False, indent=2)}{m.group(3)}"

    if RAW_RE.search(text):
        text = RAW_RE.sub(_raw, text, count=1)
    else:
        data = {"memoryBlock": block, "updatedAt": now}
        text = (
            text.rstrip()
            + "\n\n<!-- RAW_JSON_START\n"
            + json.dumps(data, ensure_ascii=False, indent=2)
            + "\nRAW_JSON_END -->\n"
        )
    return text


def write(block: str, bump_version: bool = True, force: bool = False) -> dict:
    """写入云记忆正文。返回 {ok, backup, warnings, ...}。

    `force=False` 时，如果原有正文非空且不是本工具写的，拒绝覆盖——
    那是用户自己的记忆，不能因为点了一下按钮就没了。
    """
    cur = read()
    if cur is None:
        return {"ok": False, "error": "NO_FILE", "message": "本机没有云记忆文件"}

    warnings: list[str] = []
    if cur.block and not force:
        return {
            "ok": False,
            "error": "OCCUPIED",
            "message": (
                f"云记忆里已经有 {len(cur.block)} 字符你自己的内容。"
                f"直接覆盖会丢掉它——要覆盖请先看清内容再确认。"
            ),
            "current": cur.as_dict(),
        }

    if len(block) > MEMORY_BLOCK_LIMIT:
        warnings.append(
            f"正文 {len(block)} 字符，超过云记忆窗口 {MEMORY_BLOCK_LIMIT}——"
            f"超出的部分宿主读不到，先精简"
        )

    bak = backup()
    if bak is None:
        return {"ok": False, "error": "BACKUP_FAILED", "message": "备份失败，已中止写入"}

    try:
        old_text = cur.path.read_text(encoding="utf-8", errors="replace")
        new_text = _compose(old_text, block, bump_version)
        cur.path.write_text(new_text, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": "WRITE_FAILED", "message": f"写入失败：{exc}", "backup": str(bak)}

    after = read()
    if after is None or after.block != block.strip():
        return {
            "ok": False,
            "error": "VERIFY_FAILED",
            "message": "写入后回读不一致，已保留备份，请还原后重试",
            "backup": str(bak),
        }
    if not after.raw_ok:
        warnings.append(
            "正文写进去了，但 RAW_JSON 里的 memoryBlock 与正文不一致——"
            "宿主同步的是 RAW_JSON 那份，可能出现「看着改了、注入还是旧的」"
        )

    return {
        "ok": True,
        "backup": str(bak),
        "version": after.version,
        "chars": after.chars,
        "warnings": warnings,
    }


def restore(backup_path: str) -> dict:
    """从备份还原。备份路径必须在 memory 目录内，防止拿它当任意文件覆盖的口子。"""
    src = Path(backup_path)
    try:
        src.resolve().relative_to(MEMORY_DIR.resolve())
    except ValueError:
        return {"ok": False, "error": "FORBIDDEN", "message": "备份路径不在云记忆目录内"}
    if not src.is_file():
        return {"ok": False, "error": "NOT_FOUND", "message": "备份文件不存在"}

    cur = memory_file()
    if cur is None:
        return {"ok": False, "error": "NO_FILE", "message": "本机没有云记忆文件"}

    safety = backup(tag="before-restore")
    try:
        shutil.copy2(src, cur)
    except OSError as exc:
        return {"ok": False, "error": "WRITE_FAILED", "message": f"还原失败：{exc}"}
    return {"ok": True, "restored_from": str(src), "safety_copy": str(safety) if safety else ""}


def list_backups(limit: int = 10) -> list[dict]:
    if not MEMORY_DIR.is_dir():
        return []
    items = []
    # 两种备份都要算：本工具写的 `<名>.<tag>-<时间>.bak`，以及宿主/其他工具留下的 `<名>.bak`
    for p in list(MEMORY_DIR.glob("*_memory.md.*.bak")) + list(MEMORY_DIR.glob("*_memory.md.bak")):
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({"path": str(p), "name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items[:limit]
