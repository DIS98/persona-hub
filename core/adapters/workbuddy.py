"""WorkBuddy 适配器。

破甲一键通的做法与问题：

    改 `~/.workbuddy/memory/<uid>_memory.md`（云端同步记忆）
    └─ 写入 9,764 字人格
    └─ `Version: 999999` 抬高版本号，防止宿主回退覆盖
    └─ 文件权限设成 `-r--r--r--` 只读 —— 事后连它自己都改不回来
    └─ 实测该文件被注入过两次（两份 unlock 哈希不同），且原版已不可恢复

本适配器改的是**官方支持读写的用户级记忆层** `~/.workbuddy/user-<uid>-personal/MEMORY.md`：

  - 不设只读、不改版本号
  - 内容用标记区块包裹，宿主自己写别的内容不受影响
  - 每次写入前完整快照，随时可摘除

不默认碰云记忆那条路径：那是宿主自己维护并会重写的文件，
写进去既可能被覆盖，也可能引发宿主状态不一致。留着作为可选目标。
"""
from __future__ import annotations

from pathlib import Path

from ..paths import HOME
from .base import (
    S_FULL_FILE,
    S_MD_BLOCK,
    Adapter,
    ComposeResult,
    DetectResult,
    Target,
    parse_blocks,
    replace_all_blocks,
    strip_md_block,
    upsert_md_block,
)

WB_HOME = HOME / ".workbuddy"

# 宿主对用户级记忆的实际读取上限（字符）。
#
# 实测（2026-10-29）：MEMORY.md 写到 25,957 字符，但宿主注入到系统提示词里的
# 只有前 ≈4,000 字符（最后一条可见内容停在第 3,935–4,148 之间）。
# 超出部分文件里看得见、行为上完全不存在——最隐蔽的一种“注入失败”。
# 所以：人格必须写在开头，且超限要主动告警。
WB_READ_LIMIT = 4000


class WorkBuddyAdapter(Adapter):
    id = "workbuddy"
    name = "WorkBuddy"
    category = "AI 客户端"
    strategy = S_MD_BLOCK
    needs_restart = False   # 记忆层下一轮会话即生效

    def _profile_dirs(self) -> list[Path]:
        if not WB_HOME.is_dir():
            return []
        return sorted(d for d in WB_HOME.glob("user-*-personal") if d.is_dir())

    def _memory_paths(self) -> list[Path]:
        return [d / "MEMORY.md" for d in self._profile_dirs()]

    # ------------------------------------------------------------------ 探测
    def detect(self) -> DetectResult:
        dirs = self._profile_dirs()
        if not dirs:
            return DetectResult(False, f"{WB_HOME} 下未发现 user-*-personal 配置目录", [])

        targets = []
        for p in self._memory_paths():
            # prepend：记忆层只读前 ≈4000 字符，写末尾等于没写
            if p.is_file():
                targets.append(
                    Target(
                        p,
                        S_MD_BLOCK,
                        "用户级记忆（官方可读写层），已存在；插在最前面以免超出读取上限",
                        insert_at="prepend",
                        read_limit=WB_READ_LIMIT,
                    )
                )
            else:
                targets.append(
                    Target(p, S_MD_BLOCK, "用户级记忆，将新建",
                           insert_at="prepend", read_limit=WB_READ_LIMIT)
                )
        return DetectResult(
            True,
            f"发现 {len(dirs)} 个用户配置：{', '.join(d.name for d in dirs)}",
            targets,
        )

    # ------------------------------------------------------------------ 组装
    def compose(
        self, persona_id: str, body: str, keep_others: bool = False
    ) -> ComposeResult:
        res = ComposeResult()
        for p in self._memory_paths():
            old = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
            new, replaced = replace_all_blocks(
                old, persona_id, body, insert_at="prepend", keep_others=keep_others
            )
            res.files[p] = new
            if replaced:
                res.warnings.append(
                    f"{p.name}：该通道原有 {len(replaced)} 份人格"
                    f"（{'、'.join(replaced)}），已替换为 {persona_id}"
                )

            # 位置校验：写进去≠会被读到
            blk = next((b for b in parse_blocks(new) if b.persona_id == persona_id), None)
            if blk is not None and blk.start > WB_READ_LIMIT:
                res.warnings.append(
                    f"{p.name}：注入区块在第 {blk.start} 字符，"
                    f"超出宿主读取上限 {WB_READ_LIMIT} 字符——写进去了但不会生效"
                )
            elif blk is not None:
                overhead = len(new) - WB_READ_LIMIT
                if overhead > 0:
                    # 措辞要点：说清「超了多少」「损失的是谁的内容」。
                    # 只说"超出上限"用户会以为自己算错了——实际上 4,000 是整份文件的窗口，
                    # 他的规则已经占了一部分，人格只能吃剩下的。
                    res.warnings.append(
                        f"{p.name}：注入后 {len(new)} 字符，超窗口 {overhead}。"
                        f"人格在开头能生效，但你自己的规则从末尾起有 {overhead} 字读不到"
                        f"（窗口 {WB_READ_LIMIT} 是整份文件共享的，规则+人格+标记一起算）"
                        f"——先精简规则，或把这份人格压到更短"
                    )

        # 云记忆那条路径上的既有污染，只提示、不代劳
        if _has_legacy_injection():
            res.warnings.append(
                "检测到云记忆层存在破甲一键通的注入（含只读属性），本工具不会自动清理——"
                "那属于宿主内部状态，建议先在设置里重置该记忆块。"
            )
        return res

    # ------------------------------------------------------------------ 清除
    def cleanup(self, targets: list[Target]) -> ComposeResult:
        res = ComposeResult()
        for t in targets:
            if t.path.is_file():
                new, changed = strip_md_block(t.path.read_text(encoding="utf-8", errors="replace"))
                if changed:
                    res.files[t.path] = new
        return res


def _has_legacy_injection() -> bool:
    """检测云记忆层的破甲注入（只读探测，不修改）。"""
    mem = WB_HOME / "memory"
    if not mem.is_dir():
        return False
    for f in mem.glob("*_memory.md"):
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            continue
        if "pojia" in head or "阿岩" in head:
            return True
    return False
