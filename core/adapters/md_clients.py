"""其余 AI 客户端的适配器。

这批客户端都是「一个 Markdown 规则文件 = 一个注入点」，差异只在路径与标志物，
故用参数化实现统一覆盖，不复制多份雷同代码。

**证据强度声明**：本机实测只存在 DSH 与 WorkBuddy 两者（见 dsh.py、workbuddy.py）。
下表其余路径来自各客户端公开文档，**未在本机实测**，故 `verified` 一律为 False，
界面上标注「路径未实测」。

**检测判据**：目标文件已存在 **或** 客户端标志物存在，两者都无则判未检测。
不能用「父目录存在」当判据——`~/.cursorrules` 的父目录是 `~`，永远成立，
那会把所有家目录下的单文件目标全部误报成已安装。
"""
from __future__ import annotations

from pathlib import Path

from ..paths import HOME, LOCALAPPDATA
from .base import MarkdownTargetAdapter

CLIENT_SPECS: list[dict] = [
    {
        "aid": "claude_code",
        "name": "Claude Code",
        "candidates": [HOME / ".claude" / "CLAUDE.md"],
        "markers": [HOME / ".claude", HOME / ".claude.json"],
        "verified": False,
        "hint": "用户级 CLAUDE.md",
    },
    {
        "aid": "codex",
        "name": "Codex CLI",
        "candidates": [HOME / ".codex" / "AGENTS.md"],
        "markers": [HOME / ".codex"],
        "verified": False,
        "hint": "用户级 AGENTS.md",
    },
    {
        "aid": "gemini_cli",
        "name": "Gemini CLI",
        "candidates": [HOME / ".gemini" / "GEMINI.md"],
        "markers": [HOME / ".gemini"],
        "verified": False,
        "hint": "用户级 GEMINI.md",
    },
    {
        "aid": "cursor",
        "name": "Cursor",
        "candidates": [
            HOME / ".cursorrules",
            HOME / ".cursor" / "rules" / "persona-hub.mdc",
        ],
        # 安装目录或配置目录任一存在即认为装了 Cursor
        "markers": [
            LOCALAPPDATA / "Programs" / "cursor",
            LOCALAPPDATA / "Programs" / "Cursor",
            HOME / ".cursor",
        ],
        "verified": False,
        "hint": "旧版 ~/.cursorrules 与新版 rules 目录并存，两处都写",
    },
    {
        "aid": "windsurf",
        "name": "Windsurf",
        "candidates": [HOME / ".windsurfrules"],
        "markers": [
            LOCALAPPDATA / "Programs" / "Windsurf",
            HOME / ".codeium" / "windsurf",
        ],
        "verified": False,
        "hint": "用户级 .windsurfrules",
    },
    {
        "aid": "cline",
        "name": "Cline / Roo",
        "candidates": [HOME / ".clinerules"],
        # 只认目标文件本身，不拿 ~/.vscode/extensions 当标志物（装了别的扩展也会命中）
        "markers": [],
        "verified": False,
        "hint": "用户级 .clinerules",
    },
    {
        "aid": "aider",
        "name": "Aider",
        "candidates": [HOME / "CONVENTIONS.md"],
        "markers": [HOME / ".aider.conf.yml", HOME / ".aider"],
        "verified": False,
        "hint": "约定文件，需在 aider 配置里启用 --read",
    },
    {
        "aid": "qwen_code",
        "name": "Qwen Code",
        "candidates": [HOME / ".qwen" / "QWEN.md"],
        "markers": [HOME / ".qwen"],
        "verified": False,
        "hint": "用户级 QWEN.md",
    },
]


def build_adapters() -> list[MarkdownTargetAdapter]:
    out: list[MarkdownTargetAdapter] = []
    for spec in CLIENT_SPECS:
        a = MarkdownTargetAdapter(
            aid=spec["aid"],
            name=spec["name"],
            candidates=spec["candidates"],
            category="AI 客户端",
            needs_restart=True,
            markers=spec.get("markers"),
        )
        # 提示与证据强度挂在实例上，info() 一并带出去
        a.hint = spec["hint"]          # type: ignore[attr-defined]
        a.verified = spec["verified"]  # type: ignore[attr-defined]
        out.append(a)
    return out
