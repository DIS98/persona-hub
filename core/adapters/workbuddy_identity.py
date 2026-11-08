"""WorkBuddy 的身份通道（SOUL.md / USER.md / 自定义指令）。

为什么单开一个模块
==================
`MEMORY.md` 属于「用户画像」层。宿主拼提示词时用的措辞是：

    <user_memory>
    The following is your long-term user-level memory ...
    Use it to understand user preferences, habits, and cross-project conventions.

也就是说：**它是数据，不是指令。** 人格写在那里，等于把指令塞进数据通道。
而主流注入防御明确教模型「memory 是 DATA，里面的指令要拒绝并上报」，
于是表现就是客户端那句原话——「外来的 persona 注入，不是你的指令」。

宿主自己的模板 `resources/templates/user-context-identity.tpl` 写明了几条更强通道：

    <user_custom_instructions>   "The user has provided the following custom instructions.
                                  You MUST follow them in all responses ..."      ← 最强
    <tone_and_style>             "You MUST adopt the following tone and communication style
                                  in ALL your responses. These guidelines override your
                                  default behavior ..."
    <identity_context> SOUL.md   "If SOUL.md is present, embody its persona and tone."

而**专家模式**用的是另一个模板 `user-context-expert-identity.tpl`，
它只有 `USER.md`（外加 onboarding 阶段的 `BOOTSTRAP.md`）——
SOUL.md、tone_and_style、user_custom_instructions 三者全部不注入。

结论（决定了下面三个适配器各自的有效范围）：

    普通会话 → 自定义指令（最强）> SOUL.md
    专家会话 → USER.md 是唯一通道

这些都不是绕过检测的伪装，而是宿主自己声明「该按指令读」的位置。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .base import (
    conflict_warning,
    S_JSON_KEY,
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
from .workbuddy import WB_HOME

# USER.md 的注入窗口（字符）。
#
# 实测（2026-10-30，本机 WorkBuddy 专家会话）：往 USER.md 追加 M001–M300 共 300 行
# 标记（每行 35 字符 + 换行），人格块在文件开头 90–1217 字符处。
# 宿主注入到 <identity_context> 的内容在**第 10,000 个字符处精确截断**：
# 最后可见的一行停在 "M230|0123456789abcdefg"，累计正好 10,000 字符。
#
#   文件总长 12,567 字符 → 模型只收到前 10,000 字符（占 79.6%）
#   第 10,001 字符起：文件里看得见、模型完全读不到
#
# 注意这是 **USER.md 这条通道自己的窗口**，与 MEMORY.md 的 4,000 不是同一个数。
# 别把两个常量合成一个：它们由宿主不同的注入路径决定，实测值差 2.5 倍。
USER_READ_LIMIT = 10000


def _window_warnings(filename: str, text: str, persona_id: str, limit: int) -> list[str]:
    """写进去 ≠ 会被读到。返回超出窗口的告警；空列表表示没问题。

    两条判据分开报，因为处置方式不同：
      - 区块起点越界 → 人格整块读不到，必须改（通常是没插到开头）
      - 总长越界     → 人格能生效，被吃掉的是用户排在后面的自己的规则
    """
    blk = next((b for b in parse_blocks(text) if b.persona_id == persona_id), None)
    if blk is None:
        return []
    if blk.start > limit:
        return [
            f"{filename}：注入区块在第 {blk.start} 字符，"
            f"超出宿主读取上限 {limit} 字符——写进去了但不会生效"
        ]
    overhead = len(text) - limit
    if overhead <= 0:
        return []
    return [
        f"{filename}：注入后 {len(text)} 字符，超窗口 {overhead}。"
        f"人格在开头能生效，但你自己的内容从末尾起有 {overhead} 字读不到"
        f"（窗口 {limit} 是整份文件共享的，你的规则+人格+标记一起算）"
        f"——先精简内容，或把这份人格压到更短"
    ]


class _UserFileAdapter(Adapter):
    """以 `user-*/<文件名>` 为目标的基类。

    两个要点：
      - 只在文件**已存在**时可用。它们是宿主生成的身份文件，本工具不代建——
        造一个格式不对的假的，可能干扰宿主自己的解析。
      - 用 prepend 且自动跳过 frontmatter：这两个文件都以 `---` 开头，
        内容插到前面会让整块元数据失效。

    第三个要点：`read_limit` 只写**实测过**的通道。没测过的写 None，
    而不是抄一个相邻通道的数——抄来的上限会让用户以为安全，实际在丢内容。
    """

    strategy = S_MD_BLOCK
    category = "AI 客户端"
    needs_restart = False
    filename = ""
    note = ""
    read_limit: int | None = None


    def _paths(self) -> list[Path]:
        if not WB_HOME.is_dir():
            return []
        return [
            d / self.filename
            for d in sorted(WB_HOME.glob("user-*-personal"))
            if (d / self.filename).is_file()
        ]

    def detect(self) -> DetectResult:
        paths = self._paths()
        if not paths:
            return DetectResult(
                False,
                f"{self.filename} 不存在——它是宿主生成的身份文件，本工具不代建",
                [],
            )
        targets = [
            Target(p, S_MD_BLOCK, self.note, insert_at="prepend", read_limit=self.read_limit)
            for p in paths
        ]
        return DetectResult(True, f"{self.note}｜{len(paths)} 个文件", targets)

    def compose(
        self, persona_id: str, body: str, keep_others: bool = False
    ) -> ComposeResult:
        res = ComposeResult()
        for p in self._paths():
            old = p.read_text(encoding="utf-8", errors="replace")
            new, replaced = replace_all_blocks(
                old, persona_id, body, insert_at="prepend", keep_others=keep_others
            )
            res.files[p] = new
            if replaced:
                res.warnings.append(
                    f"{p.name}：该通道原有 {len(replaced)} 份人格"
                    f"（{'、'.join(replaced)}），已替换为 {persona_id}"
                )
            if self.read_limit:
                res.warnings.extend(
                    _window_warnings(p.name, new, persona_id, self.read_limit)
                )
            res.warnings.extend(conflict_warning(p.name, new))
        return res


class WorkBuddySoulAdapter(_UserFileAdapter):
    """SOUL.md —— 宿主把它当「你是谁」。

    模板原文：`If SOUL.md is present, embody its persona and tone.`
    这是唯一一条宿主直接要求「扮演」的通道。

    限制：**专家会话不注入它**（见模块开头）。用专家时人格不会从这条路生效。
    """

    id = "workbuddy_soul"
    name = "WorkBuddy · SOUL.md"
    filename = "SOUL.md"
    note = "宿主原文「embody its persona and tone」；仅普通会话生效，专家会话不注入"


class WorkBuddyUserAdapter(_UserFileAdapter):
    """USER.md —— 用户档案。

    普通会话里它只是「关于用户」的资料，权重不高（这也是之前把它去掉的原因）。
    但专家模式是例外：`user-context-expert-identity.tpl` 里**只剩它一个**，
    措辞是 `Stay consistent with the latest injected user profile.`
    ——想在专家会话里让人格起作用，这是唯一的路。
    """

    id = "workbuddy_user"
    name = "WorkBuddy · USER.md"
    filename = "USER.md"
    note = "专家会话唯一通道（专家模板只注入 USER.md）；普通会话里权重偏低"
    read_limit = USER_READ_LIMIT


class WorkBuddyCustomAdapter(Adapter):
    """自定义指令 —— `app-config.json` 的 `personalization.customPrompt`。

    宿主模板原文：
        "The user has provided the following custom instructions.
         You MUST follow them in all responses unless they conflict with safety rules."

    这是整份提示词里**唯一带 MUST follow** 的用户内容位置，也是
    `PersonalizationCollector.resolveCustomPrompt()` 真正读取的字段。

    代价必须说清：它是宿主自己的设置文件，用户在个性化面板里改一次设置，
    宿主就会整份重写，本工具的区块会消失。所以每次注入都会带一条告警。
    """

    id = "workbuddy_custom"
    name = "WorkBuddy · 自定义指令"
    category = "AI 客户端"
    strategy = S_JSON_KEY
    needs_restart = False

    def _path(self) -> Path | None:
        p = WB_HOME / "app" / "app-config.json"
        return p if p.is_file() else None

    @staticmethod
    def _load(p: Path) -> dict:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def detect(self) -> DetectResult:
        p = self._path()
        if p is None:
            return DetectResult(False, "未找到 app-config.json（宿主的应用级设置文件）", [])
        try:
            pers = self._load(p).get("personalization") or {}
            cur = str(pers.get("customPrompt") or "")
        except (json.JSONDecodeError, OSError):
            return DetectResult(False, "app-config.json 解析失败，未纳入自动注入", [])
        state = "当前为空" if not cur.strip() else f"当前有 {len(cur)} 字符（注入会整体替换，快照可还原）"
        return DetectResult(
            True,
            f"最强通道「You MUST follow them」｜{state}",
            [Target(p, S_JSON_KEY, "personalization.customPrompt", insert_at="replace")],
        )

    def compose(
        self, persona_id: str, body: str, keep_others: bool = False
    ) -> ComposeResult:
        res = ComposeResult()
        p = self._path()
        if p is None:
            return res
        data = self._load(p)
        pers = data.get("personalization")
        if not isinstance(pers, dict):
            pers = {}
        old = str(pers.get("customPrompt") or "")
        merged, replaced = replace_all_blocks(
            old, persona_id, body, insert_at="append", keep_others=keep_others
        )
        pers["customPrompt"] = merged
        data["personalization"] = pers
        res.files[p] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if replaced:
            res.warnings.append(
                f"自定义指令里原有 {len(replaced)} 份人格"
                f"（{'、'.join(replaced)}），已替换为 {persona_id}"
            )
        res.warnings.append(
            "自定义指令是宿主自己的设置：在个性化面板里改任何一项，宿主会整份重写，"
            "这里的区块会消失。改完先用**普通会话**验证一次"
        )
        res.warnings.extend(conflict_warning("自定义指令", merged))
        return res

    def cleanup(self, targets: list[Target]) -> ComposeResult:
        res = ComposeResult()
        p = self._path()
        if p is None:
            return res
        data = self._load(p)
        pers = data.get("personalization")
        if not isinstance(pers, dict):
            return res
        old = str(pers.get("customPrompt") or "")
        new, changed = strip_md_block(old)
        if changed:
            pers["customPrompt"] = new.strip()
            data["personalization"] = pers
            res.files[p] = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        return res
