"""注入的开关与删除。

开关语义（自己写的与别人写的，对外统一，内部两条路径）：

    self         改自己区块的 state。off 时正文不写进文件——只留一行说明。
                 如果 off 时还把正文留在文件里，宿主照样读得到，"停用"就是自欺。
    third_party  把整块内容移到 data/muted/ 存好，原位只留 muted 标记。
                 留标记而不是删干净，是为了「关掉之后还能重新打开」；
                 否则开关就变成单向删除，跟破甲一键通那个假 revert 没区别。

删除语义：

    file 型   从文件中精确移除该段（字符偏移，不动同文件里的其他区块）
    dir 型    递归删除目录内文件，全部纳入同一事务

所有写操作走 Transaction：先快照、再原子写、后回读校验，任一失败全部回滚。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .adapters.base import MUTED_INLINE_RE, muted_marker, upsert_md_block
from .detect import Finding, scan_all
from .paths import data_root
from .persona import PersonaLibrary
from .txn import Transaction
from .vault import Vault


class OpError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _muted_dir() -> Path:
    p = data_root() / "muted"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _find(fid: str) -> Finding:
    for f in scan_all():
        if f.id == fid:
            return f
    raise OpError("NOT_FOUND", "该项已不存在或文件已被外部改动，请刷新后重试")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------------ 开关
def toggle(vault: Vault, library: PersonaLibrary, fid: str, enable: bool) -> dict:
    f = _find(fid)
    path = Path(f.path)

    if f.scope == "dir":
        raise OpError("NOT_TOGGLEABLE", "目录型注入不支持开关，只能移除")

    if f.kind == "anomaly":
        raise OpError("NOT_TOGGLEABLE", "这是权限提示，不是可开关的注入")

    text = _read(path)

    if f.kind == "self":
        # 自己的区块：重新渲染该人格，按目标状态重写区块
        try:
            body = library.render(f.persona_id)
        except Exception as exc:  # noqa: BLE001
            raise OpError("PERSONA_MISSING", f"人格 {f.persona_id} 已不在库中：{exc}") from exc
        if not body.strip():
            raise OpError("PERSONA_EMPTY", f"人格 {f.persona_id} 渲染结果为空")
        new_text = upsert_md_block(text, f.persona_id, body, "on" if enable else "off")
        label = f"{'enable' if enable else 'disable'}:{f.persona_id}"
    else:
        # 第三方：启用 = 把存起来的内容放回去；停用 = 把内容取出来存好
        if enable:
            new_text = _unmute(text, f)
        else:
            new_text = _mute(text, f)
        label = f"{'enable' if enable else 'disable'}:{f.source}"

    txn = Transaction(vault)
    txn.stage(path, new_text)
    res = txn.commit(label=label, actor="gui")
    if not res.ok:
        raise OpError("WRITE_FAILED", res.message)

    return {
        "ok": True,
        "snapshot_id": res.snapshot_id,
        "path": str(path),
        "state": "on" if enable else "off",
        "message": ("已启用" if enable else "已停用"),
    }


def _mute(text: str, f: Finding) -> str:
    chunk = text[f.off_start : f.off_end]
    if not chunk.strip():
        raise OpError("EMPTY_TARGET", "目标区段为空，可能文件已被外部改动")

    sha = hashlib.sha256(chunk.encode("utf-8")).hexdigest()[:16]
    lines = chunk.count("\n") + 1
    # 必须用二进制写。文本模式在 Windows 上会把 \n 改成 \r\n、把原有的 \r\n 改成 \r\r\n，
    # 存档再恢复就对不上原始字节了——而这里唯一的要求就是逐字节可逆。
    (_muted_dir() / f"{sha}.txt").write_bytes(chunk.encode("utf-8"))
    return text[: f.off_start] + muted_marker(f.source, sha, lines) + text[f.off_end :]


def _unmute(text: str, f: Finding) -> str:
    for m in MUTED_INLINE_RE.finditer(text):
        if m.group(1) != f.source:
            continue
        sha = m.group(2)
        store = _muted_dir() / f"{sha}.txt"
        if not store.is_file():
            raise OpError("MUTED_MISSING", f"停用时的内容副本已丢失：{store}")
        original = store.read_bytes().decode("utf-8")
        return text[: m.start()] + original + text[m.end() :]
    raise OpError("NOT_MUTED", "没有找到该来源的停用标记，可能已启用或已被外部改动")


# ------------------------------------------------------------------ 删除
def remove(vault: Vault, fids: list[str]) -> dict:
    if not fids:
        raise OpError("BAD_REQUEST", "未指定要移除的项")

    findings = {f.id: f for f in scan_all()}
    txn = Transaction(vault)
    touched_dirs: list[Path] = []
    planned: list[str] = []
    skipped: list[str] = []

    for fid in fids:
        f = findings.get(fid)
        if f is None:
            skipped.append(fid)
            continue

        if not f.deletable:
            skipped.append(fid)
            continue

        if f.scope == "dir":
            d = Path(f.path)
            files = [x for x in d.rglob("*") if x.is_file()]
            if not files:
                skipped.append(fid)
                continue
            for x in files:
                txn.stage(x, None)      # None = 删除
            touched_dirs.append(d)
            planned.append(fid)
            continue

        path = Path(f.path)
        text = _read(path)
        # 用当次扫描拿到的偏移精确切，不动同文件里的其他内容
        new_text = text[: f.off_start] + text[f.off_end :]
        # 清掉因移除产生的连续空行
        import re as _re

        new_text = _re.sub(r"\n{3,}", "\n\n", new_text).rstrip() + "\n"
        txn.stage(path, new_text)
        planned.append(fid)

    if not planned:
        return {"ok": True, "removed": 0, "skipped": skipped, "message": "没有可移除的项"}

    res = txn.commit(label=f"remove:{len(planned)}", actor="gui")
    if not res.ok:
        raise OpError("WRITE_FAILED", res.message)

    # 目录内的文件删完后，把空目录一并收掉（放事务之后，纯清理，失败不影响结论）
    cleaned = 0
    for d in touched_dirs:
        try:
            for sub in sorted(d.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if sub.is_dir() and not any(sub.iterdir()):
                    sub.rmdir()
                    cleaned += 1
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                cleaned += 1
        except OSError:
            pass

    return {
        "ok": True,
        "removed": len(planned),
        "skipped": skipped,
        "dirs_cleaned": cleaned,
        "snapshot_id": res.snapshot_id,
        "steps": [{"path": s.path, "status": s.status} for s in res.steps],
        "message": f"移除 {len(planned)} 项",
    }
