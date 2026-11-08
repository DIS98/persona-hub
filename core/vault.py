"""内容寻址备份库（Vault）。

设计目标——逐条对准破甲一键通的失败点：

| 破甲的失败 | 本模块的做法 |
|---|---|
| `历史备份/` 空目录，只记路径不存内容 | 写入前把**原文**存进 objects/，按 sha256 去重 |
| 无法区分「原本不存在」与「原本是空文件」 | manifest 里带 `existed` 布尔值 |
| 把文件设成只读，事后自己都改不回来 | 记录 `mode`，还原时一并恢复 |
| 覆盖式写入，旧内容永久丢失 | 每次提交前强制快照，任何快照可随时还原 |
| 无完整性校验 | 还原后逐文件回读比对 sha256 |

存储布局：
    vault/
      objects/<sha[:2]>/<sha>        原始字节，同样内容只存一份
      snapshots/<id>/
        meta.json                    时间 / 标签 / 触发者
        files.json                   条目清单
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

CHUNK = 1024 * 1024


# ------------------------------------------------------------------ 工具函数
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(path: Path) -> str:
    """统一成可比较的绝对路径字符串（Windows 下大小写不敏感）。"""
    return str(Path(path).resolve()).replace("\\", "/")


def _clear_readonly(path: Path) -> None:
    """清掉只读属性。

    Windows 上只读文件无法被 os.replace 覆盖或删除，会抛 PermissionError。
    破甲把 WorkBuddy 记忆设成只读后自己再也改不动，就是漏了这一步。
    """
    try:
        mode = os.stat(path).st_mode
        if not (mode & stat.S_IWRITE):
            os.chmod(path, mode | stat.S_IWRITE)
    except FileNotFoundError:
        pass


# ------------------------------------------------------------------ 数据结构
@dataclass
class Entry:
    path: str
    existed: bool
    sha256: str | None = None
    size: int = 0
    mode: int = 0o644

    @property
    def is_file(self) -> bool:
        return self.existed and self.sha256 is not None


@dataclass
class SnapshotMeta:
    id: str
    created_at: float
    label: str
    actor: str = "cli"
    entries: list[dict] = field(default_factory=list)

    @property
    def time_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created_at))


@dataclass
class RestoreResult:
    path: str
    action: str          # restored / deleted / already-ok / failed
    ok: bool
    detail: str = ""


# ------------------------------------------------------------------ Vault
class Vault:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.snapshots = self.root / "snapshots"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- 对象存储
    def _object_path(self, sha: str) -> Path:
        return self.objects / sha[:2] / sha

    def _put_object(self, data: bytes) -> str:
        sha = sha256_bytes(data)
        dst = self._object_path(sha)
        if dst.exists():
            return sha

        dst.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件名必须唯一：并发写入同一份内容时，固定名字会让两个线程抢同一个 tmp，
        # 一个 os.replace 走之后另一个就 PermissionError。
        tmp = dst.with_name(f"{sha}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_bytes(data)
        try:
            os.replace(tmp, dst)
        except OSError:
            # 另一线程已经放好了同名对象，内容相同，直接复用
            if not dst.exists():
                raise
            try:
                tmp.unlink()
            except OSError:
                pass
        return sha

    def _get_object(self, sha: str) -> bytes:
        p = self._object_path(sha)
        if not p.is_file():
            raise FileNotFoundError(f"对象缺失（vault 被破坏）: {sha}")
        data = p.read_bytes()
        if sha256_bytes(data) != sha:
            raise ValueError(f"对象校验失败（vault 被破坏）: {sha}")
        return data

    # ---------------------------------------------------------- 快照
    def capture(
        self,
        paths: Iterable[Path],
        label: str = "manual",
        actor: str = "cli",
        contents: dict[str, bytes] | None = None,
    ) -> str:
        """为一批路径拍快照。

        contents: 可选，{规范化路径: 预期新内容}。传入时用于「基于新内容再拍」的场景，
                  但本方法永远记录**磁盘上的现状**——那才是能还原的东西。
        """
        entries: list[Entry] = []
        for raw in paths:
            p = Path(raw)
            if p.is_file():
                data = p.read_bytes()
                sha = self._put_object(data)
                st = p.stat()
                entries.append(
                    Entry(
                        path=_norm(p),
                        existed=True,
                        sha256=sha,
                        size=len(data),
                        mode=stat.S_IMODE(st.st_mode),
                    )
                )
            else:
                # 关键：记录「原本不存在」。还原时据此删除文件，而不是写空内容。
                entries.append(Entry(path=_norm(raw), existed=False))

        snap_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        meta = SnapshotMeta(
            id=snap_id,
            created_at=time.time(),
            label=label,
            actor=actor,
            entries=[asdict(e) for e in entries],
        )
        d = self.snapshots / snap_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(
            json.dumps(asdict(meta), ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="\n",   # 固定 LF，不随平台变
        )
        return snap_id

    def load(self, snapshot_id: str) -> SnapshotMeta:
        mp = self.snapshots / snapshot_id / "meta.json"
        if not mp.is_file():
            raise FileNotFoundError(f"快照不存在: {snapshot_id}")
        raw = json.loads(mp.read_text(encoding="utf-8"))
        entries = raw.pop("entries", [])
        meta = SnapshotMeta(**raw)
        meta.entries = entries
        return meta

    def list_snapshots(self) -> list[SnapshotMeta]:
        out: list[SnapshotMeta] = []
        for d in self.snapshots.iterdir():
            if d.is_dir() and (d / "meta.json").is_file():
                try:
                    out.append(self.load(d.name))
                except Exception:
                    continue
        out.sort(key=lambda m: m.created_at, reverse=True)
        return out

    # ---------------------------------------------------------- 还原
    def _write_atomic(self, path: Path, data: bytes, mode: int | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            _clear_readonly(path)
        tmp = path.with_name(f".{path.name}.hub-tmp-{uuid.uuid4().hex[:8]}")
        try:
            tmp.write_bytes(data)
            if mode is not None:
                try:
                    os.chmod(tmp, mode)
                except OSError:
                    pass
            # os.replace 在同卷上是原子的：要么旧文件，要么新文件，不会出现半截文件
            os.replace(tmp, path)
        except OSError:
            # 失败时清掉临时文件，不在用户目录里留 `.xxx.hub-tmp-xxxxxxxx` 垃圾
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def restore(self, snapshot_id: str, only: Iterable[str] | None = None) -> list[RestoreResult]:
        """按快照还原。only 给出时只还原其中部分路径。"""
        meta = self.load(snapshot_id)
        wanted = {_norm(Path(p)) for p in only} if only else None
        results: list[RestoreResult] = []

        for raw in meta.entries:
            path_str = raw["path"]
            if wanted and path_str not in wanted:
                continue
            p = Path(path_str)
            existed = raw.get("existed", False)

            try:
                if not existed:
                    # 原本没有 → 现在若有就删掉
                    if p.exists():
                        _clear_readonly(p)
                        p.unlink()
                        results.append(RestoreResult(path_str, "deleted", True, "该文件原本不存在"))
                    else:
                        results.append(RestoreResult(path_str, "already-ok", True, "该文件原本不存在"))
                    continue

                data = self._get_object(raw["sha256"])
                # 内容已一致就不动它，避免无谓改 mtime
                if p.is_file() and sha256_file(p) == raw["sha256"]:
                    results.append(RestoreResult(path_str, "already-ok", True, "内容已一致"))
                    continue

                self._write_atomic(p, data, raw.get("mode"))
                # 回读校验——破甲那处「写入未生效（回读不一致）」就是没把校验当硬条件
                if sha256_file(p) != raw["sha256"]:
                    results.append(RestoreResult(path_str, "failed", False, "回读校验不一致"))
                else:
                    results.append(RestoreResult(path_str, "restored", True, f"{len(data)} B"))
            except Exception as exc:  # noqa: BLE001 - 单点失败不应中断整批还原
                results.append(RestoreResult(path_str, "failed", False, f"{type(exc).__name__}: {exc}"))

        return results

    # ---------------------------------------------------------- 校验 / 统计
    def verify(self, snapshot_id: str) -> list[RestoreResult]:
        """校验当前磁盘状态是否与快照一致（不写入）。"""
        meta = self.load(snapshot_id)
        out: list[RestoreResult] = []
        for raw in meta.entries:
            p = Path(raw["path"])
            if not raw.get("existed"):
                ok = not p.exists()
                out.append(RestoreResult(raw["path"], "absent" if ok else "present", ok))
                continue
            if not p.is_file():
                out.append(RestoreResult(raw["path"], "missing", False))
                continue
            same = sha256_file(p) == raw["sha256"]
            out.append(RestoreResult(raw["path"], "same" if same else "changed", same))
        return out

    def stats(self) -> dict:
        obj_count = 0
        obj_bytes = 0
        for d in self.objects.iterdir() if self.objects.exists() else []:
            if d.is_dir():
                for f in d.iterdir():
                    if f.is_file():
                        obj_count += 1
                        obj_bytes += f.stat().st_size
        snaps = self.list_snapshots()
        return {
            "objects": obj_count,
            "object_bytes": obj_bytes,
            "snapshots": len(snaps),
            "latest": snaps[0].time_str if snaps else None,
        }

    def gc(self, keep: int = 30) -> int:
        """删除旧快照并回收无引用对象。keep=保留最近 N 个快照。"""
        snaps = self.list_snapshots()
        removed = 0
        for meta in snaps[keep:]:
            import shutil

            shutil.rmtree(self.snapshots / meta.id, ignore_errors=True)
            removed += 1

        # 回收：没有任何快照引用的对象
        referenced: set[str] = set()
        for meta in self.list_snapshots():
            for e in meta.entries:
                if e.get("sha256"):
                    referenced.add(e["sha256"])

        freed = 0
        for d in list(self.objects.iterdir()) if self.objects.exists() else []:
            if not d.is_dir():
                continue
            for f in list(d.iterdir()):
                if f.is_file() and f.name not in referenced:
                    freed += f.stat().st_size
                    f.unlink()
            if not any(d.iterdir()):
                d.rmdir()
        return removed, freed
