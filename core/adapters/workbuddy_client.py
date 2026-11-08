"""WorkBuddy —— 把四条通道收进**一个**客户端条目。

为什么要合并
============
之前把 WorkBuddy 的四个落点当成四个独立客户端列出来，界面上就变成
"自定义指令 / SOUL.md / USER.md / MEMORY.md" 四行，看起来像四个软件。
实际它们是同一个客户端的四个落点，跟破甲一键通"一个客户端一行、行内显示靶点数"是两回事。

合并后：
  · 界面只出现一行 `WorkBuddy`，副标题写明「4 条通道可用」；
  · 注入时按固定优先级写入，不需要用户理解通道；
  · 想单独控制某条通道，仍可在「注入管理」里看到并停用（那里是按文件/条目列的，粒度更细）。

默认写哪几条
------------
默认写 **自定义指令 + USER.md** 两条，因为宿主是按会话类型二选一注入的：

    普通会话 → <user_custom_instructions>（"You MUST follow them"）
    专家会话 → user-context-expert-identity.tpl 里只剩 USER.md

只写一条就会有一半场景不生效，这跟"人格时灵时不灵"是同一个症状。
两条都写，覆盖两种会话；代价是普通会话里两份都会出现（后者权重低，不冲突）。

不写 MEMORY.md：那层是"关于用户的事实"，人格放那里会被判为外来注入。
不默认写 SOUL.md：它是宿主的人格底色，多份人格会叠在一起；
需要时可在界面上单独选（见 channels 参数）。
"""

from __future__ import annotations

from pathlib import Path

from .base import (
    Adapter,
    ComposeResult,
    DetectResult,
    Target,
)
from .workbuddy import WorkBuddyAdapter
from .workbuddy_identity import (
    WorkBuddyCustomAdapter,
    WorkBuddySoulAdapter,
    WorkBuddyUserAdapter,
)

# 通道顺序 = 权重顺序（依据宿主模板 user-context-identity.tpl 的措辞）
CHANNELS: tuple[tuple[str, str], ...] = (
    ("workbuddy_custom", "自定义指令"),
    ("workbuddy_soul", "SOUL.md"),
    ("workbuddy_user", "USER.md"),
    ("workbuddy_memory", "MEMORY.md"),
)

# 默认写入的通道：覆盖普通会话 + 专家会话两种模式
DEFAULT_CHANNELS = ("workbuddy_custom", "workbuddy_user")


class WorkBuddyClientAdapter(Adapter):
    """一个客户端条目，内部管四条通道。"""

    id = "workbuddy"
    name = "WorkBuddy"
    category = "AI 客户端"
    needs_restart = False
    verified = True

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _sub(channel: str) -> Adapter | None:
        if channel == "workbuddy_custom":
            return WorkBuddyCustomAdapter()
        if channel == "workbuddy_soul":
            return WorkBuddySoulAdapter()
        if channel == "workbuddy_user":
            return WorkBuddyUserAdapter()
        if channel == "workbuddy_memory":
            return WorkBuddyAdapter()
        return None

    def _available(self) -> list[tuple[str, str, list[Target]]]:
        """返回 [(channel_id, 中文名, 该通道的目标列表)]，只含真正可用的。"""
        out: list[tuple[str, str, list[Target]]] = []
        for cid, label in CHANNELS:
            sub = self._sub(cid)
            if sub is None:
                continue
            det = sub.detect()
            if det.detected and det.targets:
                out.append((cid, label, det.targets))
        return out

    # ------------------------------------------------------------------ 协议
    def detect(self) -> DetectResult:
        avail = self._available()
        if not avail:
            return DetectResult(
                False,
                "未找到 WorkBuddy 的用户配置（不在默认位置），可用「+ 自定义」手动指定",
                [],
            )

        targets: list[Target] = []
        for cid, _label, tlist in avail:
            for t in tlist:
                t.extra = {**t.extra, "channel": cid}
                targets.append(t)

        # 说明要短：侧栏两行就截断，写全反而看不到重点
        will = " + ".join(
            label for cid, label, _t in avail if cid in DEFAULT_CHANNELS
        ) or "无"
        return DetectResult(
            True,
            f"{len(avail)} 条通道 · 默认写 {will}",
            targets,
        )

    def compose(
        self,
        persona_id: str,
        body: str,
        channels: list[str] | None = None,
        keep_others: bool = False,
    ) -> ComposeResult:
        """按通道写入。channels 为空时用 DEFAULT_CHANNELS。"""
        res = ComposeResult()
        avail = {cid for cid, _l, _t in self._available()}
        want = [c for c in (channels or DEFAULT_CHANNELS) if c in avail]
        if not want:
            return res

        for cid in want:
            sub = self._sub(cid)
            if sub is None:
                continue
            part = sub.compose(persona_id, body, keep_others=keep_others)
            for p, content in part.files.items():
                res.files[p] = content
            res.warnings.extend(part.warnings)

        # 偏长提示：默认通道没有硬上限，但长不等于好——社区有实测结论
        # （1,000 token 的 bot 在准确率与保持度上优于 10,000 token 的）。
        if len(body) > 4000:
            res.warnings.append(
                f"人格正文 {len(body)} 字符，偏长。它会和各通道、对话历史抢同一个上下文窗口；"
                f"实测结论是短而准胜过长而全——先删形容词，把'要严谨'换成可判定的条件"
            )

        # 这里**不再**提示"写了两条通道"。
        # 那是设计本身（宿主按会话类型二选一注入），每次写入都提一遍等于噪音，
        # 而且它原本被前端当报错弹成红框，用户看到的第一反应是"是不是失败了"。
        # 通道设计写在界面的说明和 README 里，需要时查得到。
        return res

    def cleanup(self, targets: list[Target]) -> ComposeResult:
        """按通道逐条摘除，只动本工具写入的区块。"""
        res = ComposeResult()
        by_channel: dict[str, list[Target]] = {}
        for t in targets:
            cid = str(t.extra.get("channel") or "")
            if cid:
                by_channel.setdefault(cid, []).append(t)
        for cid, tlist in by_channel.items():
            sub = self._sub(cid)
            if sub is None:
                continue
            part = sub.cleanup(tlist)
            for p, content in part.files.items():
                res.files[p] = content
        return res
