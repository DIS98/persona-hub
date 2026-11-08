"""适配器注册表。

对外暴露：all_adapters() / get_adapter() / add_custom() / remove_custom()。
新增一个内置客户端 = 在这里加一行，不改其他任何模块。
用户自定义目标存在 data/custom-targets.json，与内置的并列参与检测与注入。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..paths import data_root
from .base import (
    S_MD_BLOCK,
    Adapter,
    ComposeResult,
    DetectResult,
    MarkdownTargetAdapter,
    Target,
    strip_md_block,
    upsert_md_block,
)
from .dsh import DshAdapter
from .md_clients import build_adapters
from .workbuddy import WorkBuddyAdapter
from .workbuddy_client import WorkBuddyClientAdapter
from .workbuddy_identity import (
    WorkBuddyCustomAdapter,
    WorkBuddySoulAdapter,
    WorkBuddyUserAdapter,
)


class CustomAdapter(MarkdownTargetAdapter):
    """用户自选任意 Markdown 文件作为注入目标。"""

    category = "自定义"
    # 用户明确指定的路径，不需要"实测"确认——那是给内置适配器的谨慎标记
    verified = True
    # 供前端区分"可以删除的自定义项"与"内置项"
    is_custom = True

    def __init__(self, aid: str, name: str, paths: list[Path]):
        super().__init__(aid=aid, name=name, candidates=paths, category="自定义", needs_restart=False)

    def detect(self) -> DetectResult:
        # 自定义目标不要求父目录存在——用户明确指定了就认
        targets = [
            Target(p, S_MD_BLOCK, "用户指定" if not p.is_file() else "用户指定（已存在）")
            for p in self._candidates
        ]
        return DetectResult(bool(targets), f"{len(targets)} 个自定义路径", targets)


def _custom_file() -> Path:
    return data_root() / "custom-targets.json"


def load_custom() -> list[CustomAdapter]:
    p = _custom_file()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[CustomAdapter] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            out.append(
                CustomAdapter(
                    aid=str(item["id"]),
                    name=str(item.get("name") or item["id"]),
                    paths=[Path(x) for x in item.get("paths", [])],
                )
            )
        except (KeyError, TypeError):
            continue
    return out


def save_custom(items: list[dict]) -> None:
    _custom_file().write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",   # 固定 LF，不随平台变
    )


def _raw_custom() -> list[dict]:
    """读出原始 JSON（保留未被本模块识别的字段，避免保存时丢信息）。"""
    p = _custom_file()
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [x for x in raw if isinstance(x, dict)] if isinstance(raw, list) else []


def next_custom_id(name: str, taken: set[str] | None = None) -> str:
    """生成一个不冲突的合法 id。

    中文名 slug 之后会变成空串或一串 "-"，直接拿来当 id 会让第二个自定义目标
    覆盖第一个——这跟人格库那个覆盖 bug 是同一个坑，所以统一在这里兜住。
    """
    base = re.sub(r"[^a-z0-9_-]+", "-", str(name or "").strip().lower()).strip("-_")
    base = base or "custom"
    base = base[:40]

    used = set(taken or ())
    used |= {a.id for a in all_adapters()}          # 内置客户端也不许撞

    if base not in used:
        return base
    n = 2
    while f"{base}-{n}" in used:
        n += 1
    return f"{base}-{n}"


def add_custom(name: str, paths: list[str], cid: str = "") -> CustomAdapter:
    """新增或更新一个自定义目标。id 留空则自动分配唯一值。"""
    clean = [str(p).strip() for p in paths if str(p).strip()]
    if not clean:
        raise ValueError("至少需要一个文件路径")
    # 目录不是可写入的"文件目标"。早先允许加进去，然后在 apply 时才失败
    # （"写入目录 -> ok:false, Permission denied"）——用户加了一个明显用不了的目标，
    # 界面却先点头，等到写入才翻脸。这里当场拒掉。
    for p in clean:
        if Path(p).is_dir():
            raise ValueError(f"这是目录，不是文件：{p}（请填具体到 .md 的文件路径）")

    items = _raw_custom()
    existing_ids = {str(x.get("id") or "") for x in items}

    want = str(cid or "").strip()
    if want and re.fullmatch(r"[A-Za-z0-9_\-]{1,48}", want):
        aid = want
    else:
        # 不在已有自定义 id 里判断即可——next_custom_id 内部已排除内置 id，
        # 但自定义的 id 也要算作"已占用"，否则会和别的自定义项撞
        aid = next_custom_id(name, taken=existing_ids)

    items = [x for x in items if str(x.get("id") or "") != aid]
    items.append({"id": aid, "name": str(name or aid).strip() or aid, "paths": clean})
    save_custom(items)
    return CustomAdapter(aid=aid, name=str(name or aid).strip() or aid, paths=[Path(p) for p in clean])


def remove_custom(cid: str) -> bool:
    """删除一个自定义目标。返回是否真的删掉了。"""
    items = _raw_custom()
    left = [x for x in items if str(x.get("id") or "") != cid]
    if len(left) == len(items):
        return False
    save_custom(left)
    return True


def all_adapters() -> list[Adapter]:
    # 一个客户端 = 一个条目。WorkBuddy 内部有 4 条通道（自定义指令 / SOUL.md /
    # USER.md / MEMORY.md），但界面只该出现一行——把它拆成四行，用户看到的是
    # "同一个软件出现四次"，而不是"一个软件有四个落点"。
    # 通道的强度与取舍写在 workbuddy_client.py 开头。
    adapters: list[Adapter] = [WorkBuddyClientAdapter(), DshAdapter()]
    adapters.extend(build_adapters())
    adapters.extend(load_custom())
    return adapters


def get_adapter(aid: str) -> Adapter | None:
    for a in all_adapters():
        if a.id == aid:
            return a
    return None


__all__ = [
    "Adapter",
    "ComposeResult",
    "DetectResult",
    "Target",
    "add_custom",
    "all_adapters",
    "get_adapter",
    "load_custom",
    "next_custom_id",
    "remove_custom",
    "save_custom",
    "strip_md_block",
    "upsert_md_block",
]
