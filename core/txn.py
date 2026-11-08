"""写入事务：预演 → 快照 → 原子写 → 回读校验 → 失败自动回滚。

这是与破甲一键通最本质的区别。它的写入是「直接覆盖 + 设只读 + 概不负责」，
本模块的写入是一条**全或无**的事务：

    1. stage   只登记，不落盘          → 用户可以只看 diff 就取消
    2. preview 生成 unified diff       → 写什么、改哪几行，写之前看得见
    3. commit  快照现状 → 逐个原子写 → 逐个回读校验
    4. 任一环节失败 → 用刚拍的快照把已写的全部还原

破甲那处「写入未生效（回读不一致）: acc-product-config-v3.json」在日志里只是一行提示，
后面照常继续。本模块遇到这种情况会让整笔事务失败并回滚——半成功比失败更危险。
"""
from __future__ import annotations

import difflib
import os
import stat
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .paths import log_path
from .vault import Vault, _clear_readonly, _norm, sha256_bytes

# 按路径加锁。
# 为什么需要：commit 末尾会「回读校验」，并发写同一文件时会出现
#   A 写完 w0 → B 立刻写 w1 → A 回读发现是 w1 ≠ w0 → 判为失败并回滚
# 结果谁都没写成功，文件退回原样，而且**不抛异常**，最难发现。
# 按路径串行化后，回读校验才能拿到确定的结果。
_path_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lk = _path_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _path_locks[key] = lk
        return lk


def _as_bytes(content: bytes | str) -> bytes:
    if isinstance(content, bytes):
        return content
    return content.encode("utf-8")


def _text_for_diff(data: bytes | None) -> str:
    if data is None:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # 非 UTF-8 内容不参与文本 diff，用占位避免抛错
        return f"<二进制 {len(data)} B>"


@dataclass
class Staged:
    path: Path
    new: bytes | None          # None 表示删除该文件（摘除注入）
    old: bytes | None          # None 表示文件原本不存在
    existed: bool

    @property
    def is_delete(self) -> bool:
        return self.new is None

    @property
    def changed(self) -> bool:
        if self.is_delete:
            return self.existed
        if not self.existed:
            return True
        return sha256_bytes(self.old or b"") != sha256_bytes(self.new)


@dataclass
class StepResult:
    path: str
    status: str            # written / unchanged / failed / rolled-back
    detail: str = ""


@dataclass
class CommitResult:
    ok: bool
    snapshot_id: str | None = None
    steps: list[StepResult] = field(default_factory=list)
    message: str = ""

    @property
    def written(self) -> int:
        return sum(1 for s in self.steps if s.status == "written")


class Transaction:
    def __init__(self, vault: Vault):
        self.vault = vault
        self._staged: dict[str, Staged] = {}
        self.txid = uuid.uuid4().hex[:8]

    # ---------------------------------------------------------------- 暂存
    def stage(self, path: Path | str, content: bytes | str | None) -> Staged:
        """content 传 None 表示删除该文件（用于摘除注入）。"""
        p = Path(path)
        key = _norm(p)
        if p.is_file():
            old = p.read_bytes()
            existed = True
        else:
            old, existed = None, False
        new = None if content is None else _as_bytes(content)
        st = Staged(path=p, new=new, old=old, existed=existed)
        self._staged[key] = st
        return st

    def staged(self) -> list[Staged]:
        return list(self._staged.values())

    def changed(self) -> list[Staged]:
        return [s for s in self._staged.values() if s.changed]

    def discard(self) -> None:
        self._staged.clear()

    # ---------------------------------------------------------------- 预演
    def preview(self, max_lines: int = 200) -> list[dict]:
        """返回每个目标的变化描述 + unified diff。不写任何文件。"""
        out: list[dict] = []
        for st in self.staged():
            if not st.changed:
                out.append(
                    {
                        "path": _norm(st.path),
                        "action": "unchanged",
                        "summary": "内容已一致，无需写入",
                        "diff": "",
                        "added": 0,
                        "removed": 0,
                    }
                )
                continue

            if st.is_delete:
                diff_lines = list(
                    difflib.unified_diff(
                        _text_for_diff(st.old).splitlines(keepends=True),
                        [],
                        fromfile=_norm(st.path),
                        tofile="/dev/null",
                        n=2,
                    )
                )
                removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
                out.append(
                    {
                        "path": _norm(st.path),
                        "action": "delete",
                        "summary": f"删除文件，-{removed} 行",
                        "diff": "".join(diff_lines[:max_lines]),
                        "added": 0,
                        "removed": removed,
                    }
                )
                continue

            old_text = _text_for_diff(st.old)
            new_text = _text_for_diff(st.new)
            diff_lines = list(
                difflib.unified_diff(
                    old_text.splitlines(keepends=True),
                    new_text.splitlines(keepends=True),
                    fromfile=_norm(st.path) if st.existed else "/dev/null",
                    tofile=_norm(st.path),
                    n=2,
                )
            )
            added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
            removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))

            if not st.existed:
                action = "create"
            elif st.new == b"" and not st.old:
                action = "unchanged"
            else:
                action = "modify"

            out.append(
                {
                    "path": _norm(st.path),
                    "action": action,
                    "summary": f"+{added} / -{removed} 行，{len(st.old or b'')} B → {len(st.new)} B",
                    "diff": "".join(diff_lines[:max_lines]),
                    "added": added,
                    "removed": removed,
                }
            )
        return out

    # ---------------------------------------------------------------- 提交
    def commit(self, label: str = "apply", actor: str = "cli") -> CommitResult:
        todo = self.changed()
        if not todo:
            return CommitResult(ok=True, steps=[], message="没有需要写入的变更")

        # 按路径排序后依次加锁（固定顺序避免死锁），保证同一文件的写入串行
        keys = sorted({_norm(s.path) for s in todo})
        locks = [_lock_for(k) for k in keys]
        for lk in locks:
            lk.acquire()
        try:
            return self._commit_locked(todo, label, actor)
        finally:
            for lk in reversed(locks):
                lk.release()

    def _commit_locked(self, todo: list[Staged], label: str, actor: str) -> CommitResult:
        # 拿到锁之后重新看一眼现状：可能在被等待期间已被别人改过，
        # 那时本次的「新内容」其实已经等于磁盘内容，无需再写。
        todo = [s for s in todo if s.changed]
        if not todo:
            return CommitResult(ok=True, steps=[], message="没有需要写入的变更（等待锁期间已被他人改过）")

        # 1) 先拍快照——顺序不能反。破了这步，回滚就没有依据。
        snapshot_id = self.vault.capture([s.path for s in todo], label=label, actor=actor)

        steps: list[StepResult] = []
        written: list[Staged] = []

        try:
            for st in todo:
                try:
                    self._write_one(st)
                    written.append(st)
                    steps.append(
                        StepResult(
                            _norm(st.path),
                            "deleted" if st.is_delete else "written",
                            "已删除" if st.is_delete else f"{len(st.new or b'')} B",
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    raise RuntimeError(f"{_norm(st.path)}: {type(exc).__name__}: {exc}") from exc

            # 2) 全部写完后统一回读校验（删除的项校验「确实不存在」）
            for st in written:
                if st.is_delete:
                    if st.path.exists():
                        raise RuntimeError(f"{_norm(st.path)}: 删除未生效")
                elif st.path.read_bytes() != st.new:
                    raise RuntimeError(f"{_norm(st.path)}: 回读不一致")

        except Exception as exc:  # noqa: BLE001
            # 3) 任一失败 → 整笔回滚
            for r in self.vault.restore(snapshot_id):
                steps.append(StepResult(r.path, "rolled-back" if r.ok else "failed", r.detail))
            self._log("FAIL", label, actor, f"{exc}｜已回滚至快照 {snapshot_id}")
            return CommitResult(ok=False, snapshot_id=snapshot_id, steps=steps, message=str(exc))

        self._log("OK", label, actor, f"{len(written)} 个文件｜快照 {snapshot_id}")
        return CommitResult(ok=True, snapshot_id=snapshot_id, steps=steps, message=f"写入 {len(written)} 个文件")

    # ---------------------------------------------------------------- 内部
    def _write_one(self, st: Staged) -> None:
        p = st.path
        if st.is_delete:
            if p.exists():
                _clear_readonly(p)
                p.unlink()
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        # 只读属性是用户写的"别动我"。写入前必须清掉才能替换，但清掉之后要还原：
        # 早先只清不还原，实测结果是"写成功、内容变了、只读位也没了"——
        # 用户拿只读当保护，工具却把保护一起删了，而且界面上显示的是"写入 1 个文件"。
        mode_before: int | None = None
        if p.exists():
            try:
                mode_before = stat.S_IMODE(p.stat().st_mode)
            except OSError:
                mode_before = None
            _clear_readonly(p)
        tmp = p.with_name(f".{p.name}.hub-tmp-{self.txid}")
        try:
            tmp.write_bytes(st.new or b"")
            os.replace(tmp, p)
            if mode_before is not None:
                try:
                    os.chmod(p, mode_before)
                except OSError:
                    # 内容已写成功。属性恢复失败不该让整笔事务回滚——
                    # 回滚会把用户刚确认的内容也拿走，代价大于"只读位没还原"。
                    pass
        except OSError:
            # 写失败必须把临时文件清掉：否则会在用户的配置目录里留下
            # `.xxx.hub-tmp-xxxxxxxx` 这种垃圾，而且没人知道该不该删。
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _log(self, status: str, label: str, actor: str, detail: str) -> None:
        line = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{status}] "
            f"tx={self.txid} label={label} actor={actor} {detail}\n"
        )
        try:
            with open(log_path(), "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
