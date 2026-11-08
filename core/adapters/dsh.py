"""DSH（DeepSeek Harness）适配器。

破甲一键通的做法与问题：

    改 `node_modules/@deepseek-ai/*/cordis.patch.yml`（5 个程序包文件）
    └─ 在文件中间插入 200 行 `personaPrefix: |-` 块标量，每行缩进 8 空格
    └─ 风险：缩进错一格 YAML 就崩，DSH 直接起不来；npm 重装即被覆盖

    `passport.json` 里设计了 `history` 字段，实测是空数组 —— 备份机制没实现。

本适配器的做法：**只用官方扩展点，一个字节都不碰安装目录。**

  1. `~/.dsh/AGENTS.md`                用户级规则文件，DSH 每轮自动载入（唯一生效通道）
  2. `~/.dsh/managed-prompts/<id>/`    留存副本，**DSH 当前版本不读这里**

关于第 2 条（2026-11-05 实测，别再当成"官方托管目录"）
------------------------------------------------------
对整个 DSH 安装目录（`./ Harness\\resources\\app\\dsh`）做全量字符串搜索，
`managed-prompts` **0 命中**。DSH 真正实现的指令加载器是
`@deepseek-ai/dsh-agent-instructions`，它读的是：

    $DSH_HOME/AGENTS.md  →  <agentsHome>/AGENTS.md  →  项目根到 cwd 的每一级 AGENTS.md

证据在会话记录里：每轮有一条 `user/message`，`data.source.kind == "agent-instructions"`，
正文以 `Instructions from: ~/.dsh/AGENTS.md` 开头。`managed-prompts/persona.md` 从未出现在
任何一轮的注入内容里。

所以第 2 条继续写，只是当"人格正文的独立存档"（换宿主、迁移、比对 hash 时有用），
不再声称它会被 DSH 读到。判定这一轮到底读到了什么，走 `core/verify_dsh.py`。

刻意不做的事：不改 node_modules、不改 profiles 的 cordis.patch.yml。
理由是那两处改动会与程序升级/其他工具冲突，而收益只是「生效位置更底层」，
用 AGENTS.md 已经能达成同样的注入效果。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..paths import HOME
from .base import (
    S_FULL_FILE,
    S_JSON_KEY,
    S_MD_BLOCK,
    Adapter,
    ComposeResult,
    DetectResult,
    Target,
    md_block,
    strip_md_block,
    upsert_md_block,
)

DSH_HOME = HOME / ".dsh"


class DshAdapter(Adapter):
    id = "dsh"
    name = "DSH（DeepSeek Harness）"
    category = "AI 客户端"
    strategy = S_MD_BLOCK
    needs_restart = True

    MANAGED_ID = "persona-hub"

    def _agents_md(self) -> Path:
        return DSH_HOME / "AGENTS.md"

    def _managed_dir(self) -> Path:
        return DSH_HOME / "managed-prompts" / self.MANAGED_ID

    def _passport(self) -> Path:
        return self._managed_dir() / "passport.json"

    def _persona_md(self) -> Path:
        return self._managed_dir() / "persona.md"

    # ------------------------------------------------------------------ 探测
    def detect(self) -> DetectResult:
        if not DSH_HOME.is_dir():
            return DetectResult(False, f"{DSH_HOME} 不存在，未安装 DSH", [])

        targets = [
            Target(self._agents_md(), S_MD_BLOCK, "用户级规则文件，每轮自动载入（唯一生效通道）"),
            Target(self._persona_md(), S_FULL_FILE, "人格正文存档（DSH 当前版本不读 managed-prompts）"),
            Target(self._passport(), S_JSON_KEY, "登记信息（同上，仅供本工具与迁移使用）"),
        ]
        exists = self._agents_md().is_file()
        return DetectResult(
            True,
            f"{DSH_HOME} 存在" + ("，AGENTS.md 已就位" if exists else "，AGENTS.md 将新建"),
            targets,
        )

    # ------------------------------------------------------------------ 组装
    def compose(
        self, persona_id: str, body: str, keep_others: bool = False
    ) -> ComposeResult:
        res = ComposeResult()

        # 1) AGENTS.md：带标记区块，幂等
        old = self._agents_md().read_text(encoding="utf-8", errors="replace") if self._agents_md().is_file() else ""
        res.files[self._agents_md()] = upsert_md_block(old, persona_id, body)

        # 2) 托管目录：正文原样，外加一份登记（存档用途，DSH 不读；见模块开头实测）
        res.files[self._persona_md()] = body.rstrip() + "\n"
        res.files[self._passport()] = json.dumps(
            {
                "tool": "persona-hub",
                "version": "1.0.0",
                "target": "dsh",
                "root": str(DSH_HOME),
                "persona_id": persona_id,
                "persona_chars": len(body),
                "mode": "policy",
                "last_action": "apply",
                "note": "由人格中枢写入。DSH 不读本目录（实测），生效的是 ~/.dsh/AGENTS.md；"
                        "这里只是人格正文的独立存档，供迁移与 hash 比对。"
                        "卸载请用 persona-hub 的还原功能。",
            },
            ensure_ascii=False,
            indent=2,
        )

        res.warnings.append(
            "DSH 生效通道只有 ~/.dsh/AGENTS.md 一路；改的是磁盘文件，"
            "运行中的进程不会重新读取，需要完全退出再打开"
        )
        return res

    # ------------------------------------------------------------------ 清除
    def cleanup(self, targets: list[Target]) -> ComposeResult:
        res = ComposeResult()

        p = self._agents_md()
        if p.is_file():
            new, changed = strip_md_block(p.read_text(encoding="utf-8", errors="replace"))
            if changed:
                res.files[p] = new

        for f in (self._passport(), self._persona_md()):
            if f.is_file():
                res.files[f] = ""   # 空内容 = 由事务层删除
        return res
