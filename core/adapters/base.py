"""适配器协议与通用注入策略。

破甲一键通只实现了一种注入方式：直接改写目标文件。
本模块把「怎么注入」抽象成可替换的策略，每个策略都满足两条硬约束：

  1. **幂等**：同一份人格重复注入，结果必须是同一个字节序列（不是越注越多）
  2. **可摘除**：注入的内容被唯一标记包裹，摘除后文件应回到注入前的形态

四种策略：
  markdown_block  用 HTML 注释包裹的区块，插在 Markdown 文件里（最通用）
  yaml_append     在 YAML 顶层追加一个带注释标记的条目
  json_key        结构化改 JSON 字段（标准库 json，无需第三方）
  full_file       整个文件即人格（如 CLAUDE.md），原文件进 vault 后可整体还原
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# 策略常量
S_MD_BLOCK = "markdown_block"
S_YAML_APPEND = "yaml_append"
S_JSON_KEY = "json_key"
S_FULL_FILE = "full_file"

MARK_BEGIN = "persona-hub:begin"
MARK_END = "persona-hub:end"

MD_BEGIN_RE = re.compile(r"<!--\s*persona-hub:begin.*?-->", re.S)
MD_END_RE = re.compile(r"<!--\s*persona-hub:end\s*-->")
# 完整区块：从 begin 到 end 之间的**全部正文**，一并删除。
# 只删两个标记行而留下中间内容，是最隐蔽的失败形态：看起来摘除了，内容还在。
MD_BLOCK_RE = re.compile(
    r"<!--\s*persona-hub:begin.*?-->.*?<!--\s*persona-hub:end\s*-->",
    re.S,
)
HASH_RE = re.compile(r"hash=([0-9a-f]{8})")


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


# 正文里出现本工具的标记语法时，把它中和掉——否则人格正文能**伪造区块结构**。
#
# 实测（2026-10-31 对抗探测）：正文里含 `<!-- persona-hub:end -->` 时，写出来的文件
# 变成 begin×2 / end×2，中间那句"看起来在区块外"的内容真的跑到了区块外；
# 更糟的是 `upsert_md_block` 的残缺判定靠 `text.count(MARK_BEGIN)`，
# 计数对不上就调 `strip_md_block` **把整份注入全清**——用户的人格会莫名消失，
# 而所有既有测试都是绿的（没人往正文里塞标记）。
#
# 处理方式：把标记里的半角冒号换成全角冒号。人读仍是 persona-hub：begin，
# 正则不再命中，区块结构不可能被正文破坏。
_FAKE_MARK_RE = re.compile(r"persona-hub:(muted-end|muted|begin|end)")


def neutralize_markers(text: str) -> str:
    """把正文里伪造的区块标记改成不会被识别成标记的形态。"""
    return _FAKE_MARK_RE.sub(lambda m: f"persona-hub\uff1a{m.group(1)}", text)


@dataclass
class Target:
    """一个具体落点。"""

    path: Path
    strategy: str
    note: str = ""
    extra: dict = field(default_factory=dict)
    # 注入位置：append=文件末尾（默认），prepend=frontmatter 之后（无则文件开头）
    #
    # 为什么需要区分：目标文件的内容不总是全部会被读到。宿主记忆层只注入前几千字符，
    # 末尾注入等于扔进黑洞——文件里看得见，行为上完全不存在。
    insert_at: str = "append"
    # 宿主读取上限（字符）。给定时 compose 会校验区块是否落在读取范围内，
    # 超出就在 warnings 里明说「写进去了但不会生效」，而不是静默丢弃。
    read_limit: int | None = None


@dataclass
class DetectResult:
    detected: bool
    evidence: str
    targets: list[Target] = field(default_factory=list)


@dataclass
class ComposeResult:
    """compose 的产物：路径 → 新内容。"""

    files: dict[Path, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 区块工具
# 完整区块正则：一次拿全 id / hash / state / 正文，不靠两次独立匹配拼位置
BLOCK_FULL_RE = re.compile(
    r"<!--\s*persona-hub:begin\s+id=([^\s]+)\s+hash=([0-9a-f]{8})"
    r"(?:\s+state=(on|off))?\s*-->(.*?)<!--\s*persona-hub:end\s*-->",
    re.S,
)

OFF_NOTICE = "（人格中枢：此人格已停用。内容仍在人格库里，可在工具中重新启用。）"

# 停用的第三方注入：内容被移出文件、存进 vault 旁的 muted 目录，原位只留这个标记。
# 留标记而不是删干净，是为了「关掉之后还能重新打开」——否则开关就变成单向删除。
MUTED_INLINE_RE = re.compile(
    r"<!--\s*persona-hub:muted\s+source=([^\s]+)\s+sha=([0-9a-f]{16})\s+lines=(\d+)\s*-->"
    r"\s*<!--\s*persona-hub:muted-end\s*-->",
    re.S,
)


def muted_marker(source: str, sha: str, lines: int) -> str:
    return (
        f"<!-- persona-hub:muted source={source} sha={sha} lines={lines} -->"
        f"<!-- persona-hub:muted-end -->"
    )


@dataclass
class Block:
    """文件中已识别的一个本工具注入区块。"""

    persona_id: str
    hash: str
    state: str          # on / off
    start: int          # 在文件文本中的字符起止位置
    end: int
    text: str           # 含标记的完整区块原文
    body: str           # 标记之间的内容


def conflict_warning(filename: str, text: str) -> list[str]:
    """同一个文件里同时挂着 ≥2 个人格时报警。

    为什么必须报：这些区块是**并列注入**的，宿主会一次读到好几个身份。
    实测踩过（2026-10-30）：USER.md 里同时有「行为基线」「无立场执行体」
    「色情写手」「标识器」四个，表现就是"有时行、有时不灵"——
    模型在几套互相矛盾的指令之间摇摆，而不是某一套稳定生效。

    这类失败最容易被误判成"注入没成功"，所以要在写入那一刻就说清。
    """
    ids = re.findall(r"<!--\s*persona-hub:begin\s+id=([\w-]+)", text)
    if len(ids) < 2:
        return []
    uniq = list(dict.fromkeys(ids))
    return [
        f"{filename}：现在同时挂着 {len(ids)} 个人格（{'、'.join(uniq)}）。"
        f"它们是并列注入的，宿主会一次读到多个互相矛盾的身份——"
        f"表现是「有时像 A、有时像 B」，而不是某一套稳定生效。"
        f"要稳定就只留一个（其余在「注入管理」里停用或移除），或用「替换」写一次"
    ]


def md_block(persona_id: str, body: str, state: str = "on") -> str:
    """生成带标记的 Markdown 区块。

    state=off 时不写正文，只留一行说明：
    否则“停用”只是自欺——宿主依旧会把正文读进上下文，根本没停。
    """
    inline = body.rstrip()
    # 先中和正文里的伪造标记，再算 hash —— 顺序反了会让 hash 与文件内容对不上，
    # verify 会把"自己刚写的文件"判成不一致。
    #
    # hash **必须与 state 无关**：它进 finding id，id 随状态变化的话，
    # 界面上"停用 → 启用"这一步就会拿着旧 id 报"该项已不存在"（实测打挂过 test_edge）。
    normalized = neutralize_markers(inline)
    inner = normalized if state == "on" else OFF_NOTICE
    h = short_hash(normalized)
    attr = "" if state == "on" else " state=off"
    return (
        f"<!-- {MARK_BEGIN} id={persona_id} hash={h}{attr} -->\n"
        f"{inner}\n"
        f"<!-- {MARK_END} -->\n"
    )


def parse_blocks(text: str) -> list[Block]:
    """找出文本里全部本工具的注入区块（含被停用的）。"""
    out: list[Block] = []
    for m in BLOCK_FULL_RE.finditer(text):
        out.append(
            Block(
                persona_id=m.group(1),
                hash=m.group(2),
                state=m.group(3) or "on",
                start=m.start(),
                end=m.end(),
                text=m.group(0),
                body=m.group(4),
            )
        )
    return out


def strip_md_block(text: str) -> tuple[str, bool]:
    """移除已注入区块。返回 (新文本, 是否移除过)。

    两种情形：
      - 完整区块 → 连正文一起删
      - 只剩半个标记（上次写入被打断）→ 只删标记行，保留正文，不误删
    """
    if MARK_BEGIN not in text and MARK_END not in text:
        return text, False

    new = MD_BLOCK_RE.sub("", text)
    new = MD_BEGIN_RE.sub("", new)
    new = MD_END_RE.sub("", new)
    new = re.sub(r"\n{3,}", "\n\n", new)
    return new, True


def remove_one_block(text: str, blk: Block) -> str:
    """精确移除单个区块，其他区块不动。"""
    out = text[: blk.start] + text[blk.end :]
    return re.sub(r"\n{3,}", "\n\n", out)


def replace_all_blocks(
    text: str,
    persona_id: str,
    body: str,
    state: str = "on",
    insert_at: str = "append",
    keep_others: bool = False,
) -> tuple[str, list[str]]:
    """同通道只留这一份：先摘掉本工具的所有区块，再写入当前人格。

    返回 (新文本, 被替换掉的其它人格 id)。

    为什么需要「先全摘再写」：`upsert_md_block` 只按 id 就地替换，换成另一个人格
    就会**追加**。结果是同一个文件里躺着两份人格，宿主一次读到两个身份，
    行为互相打架——用户看到的现象是"注入了新人格，但说话还是旧人格"。
    """
    if keep_others:
        # 显式要求共存：只做同 id 替换，别的区块一律不动
        return upsert_md_block(text, persona_id, body, state, insert_at), []

    blocks = parse_blocks(text)
    replaced = [b.persona_id for b in blocks if b.persona_id != persona_id]

    rest = text
    # 从后往前删：前面的区块下标不受影响
    for blk in reversed(blocks):
        rest = rest[: blk.start] + rest[blk.end :]
    rest = re.sub(r"\n{3,}", "\n\n", rest)

    return upsert_md_block(rest, persona_id, body, state, insert_at), replaced


def _frontmatter_end(text: str) -> int:
    """可以安全插入的位置：有 frontmatter 就插在它之后，否则文件最开头。

    frontmatter 必须在文件第一行。往它前面插内容会让整块元数据失效——
    USER.md 就是这种结构，一旦打坏，宿主会把它当普通正文。
    """
    if not text.startswith("---"):
        return 0
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return len("\n".join(lines[: i + 1])) + 1
    return 0


def upsert_md_block(
    text: str,
    persona_id: str,
    body: str,
    state: str = "on",
    insert_at: str = "append",
) -> str:
    """幂等写入：同 id 区块就地替换；没有则插入。

    关键区分（踩过的坑）：文件里「有本工具的标记」不等于「标记残缺」。
    早先这里只要看到 MARK_BEGIN 就认为上次写入被打断，调用 strip_md_block 全清 ——
    结果是注入第二个人格时，把文件里第一个人的区块一起删了，
    表现出来就是「多人格互相覆盖」。
    只有「有头没尾 / 有尾没头」才算残缺，这时才该清。

    insert_at="prepend"：插到 frontmatter 之后（无则文件最开头）。
    为什么需要这个模式：目标文件的所有内容不总是都会被读到。宿主记忆层只注入
    前几千字符，末尾注入等于扔进黑洞——文件里看得见，行为上完全不存在。
    注意前置时必须**先把旧位置的同 id 区块摘掉**，否则会变成两个同名区块，
    而生效的还是末尾那个，等于白改。
    """
    block = md_block(persona_id, body, state)
    existing = [b for b in parse_blocks(text) if b.persona_id == persona_id]

    if insert_at == "prepend":
        rest = text
        for blk in reversed(existing):
            rest = remove_one_block(rest, blk)
        rest = re.sub(r"\n{3,}", "\n\n", rest)   # 摘掉尾部区块后可能留下多余空行

        pos = _frontmatter_end(rest)
        head = rest[:pos].strip("\n")
        tail = rest[pos:].strip("\n")
        parts = [p for p in (head, block.strip("\n"), tail) if p]
        return "\n\n".join(parts) + "\n"

    # ---- append（默认）
    # 1) 同 id 的区块就地替换，其余原样保留
    if existing:
        blk = existing[0]
        return text[: blk.start] + block + text[blk.end :].lstrip("\n")

    # 2) 没有同 id 区块：只有标记数量对不上（残缺）时才清理
    complete = len(parse_blocks(text))
    if text.count(MARK_BEGIN) != complete or text.count(MARK_END) != complete:
        cleaned, _ = strip_md_block(text)
        return cleaned.rstrip() + "\n\n" + block

    # 3) 干净追加：文件里可能已经有别人的完整区块，一律不动
    if not text.strip():
        return block
    return text.rstrip() + "\n\n" + block


def has_md_block(text: str) -> bool:
    return MARK_BEGIN in text


# ---------------------------------------------------------------- 基类
class Adapter:
    """所有适配器的基类。子类至少要覆盖 id/name/strategy/detect/compose。"""

    id: str = ""
    name: str = ""
    strategy: str = S_MD_BLOCK
    category: str = "通用"
    # 该适配器是否需要重启宿主才生效
    needs_restart: bool = False

    def detect(self) -> DetectResult:
        raise NotImplementedError

    def compose(
        self, persona_id: str, body: str, keep_others: bool = False
    ) -> ComposeResult:
        raise NotImplementedError

    # -------------------------------------------------- 可选：清除注入
    def cleanup(self, targets: list[Target]) -> ComposeResult:
        """默认实现：把 markdown 区块摘掉。子类可覆盖。"""
        res = ComposeResult()
        for t in targets:
            if not t.path.is_file():
                continue
            if t.strategy == S_MD_BLOCK:
                text = t.path.read_text(encoding="utf-8", errors="replace")
                new, changed = strip_md_block(text)
                if changed:
                    res.files[t.path] = new
        return res

    def info(self) -> dict:
        d = self.detect()
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "strategy": self.strategy,
            "detected": d.detected,
            "evidence": d.evidence,
            "needs_restart": self.needs_restart,
            "targets": [
                {"path": str(t.path), "strategy": t.strategy, "note": t.note} for t in d.targets
            ],
        }


# ---------------------------------------------------------------- 参数化通用适配器
class MarkdownTargetAdapter(Adapter):
    """面向「一个 Markdown 规则文件就是一个目标」的客户端。

    Claude Code / Cursor / Codex / Gemini / Windsurf 等都吃这一套，
    差异只在路径，故用一个参数化实现覆盖，避免复制五份一样的代码。
    """

    strategy = S_MD_BLOCK

    def __init__(
        self,
        *,
        aid: str,
        name: str,
        candidates: list[Path],
        category: str = "通用",
        needs_restart: bool = True,
        markers: list[Path] | None = None,
    ):
        self.id = aid
        self.name = name
        self.category = category
        self.needs_restart = needs_restart
        self._candidates = [Path(c) for c in candidates]
        # 客户端存在性标志物（安装目录 / 配置目录 / 扩展目录）
        self._markers = [Path(m) for m in (markers or [])]

    def detect(self) -> DetectResult:
        """判定依据是【目标文件已存在】或【客户端标志物存在】。

        不能只看「父目录存在」：`~/.cursorrules` 的父目录就是 `~`，永远存在，
        那会把所有「家目录下的单文件目标」全判成已安装——假阳性和假阴性一样有害。
        """
        target_exists = any(c.is_file() for c in self._candidates)
        hit_marker = next((m for m in self._markers if m.exists()), None)

        if not target_exists and hit_marker is None:
            return DetectResult(False, "未检测到客户端（标志物与目标文件都不存在）", [])

        found: list[Target] = []
        evidence: list[str] = []
        if hit_marker is not None:
            evidence.append(f"客户端标志物存在：{hit_marker}")

        for c in self._candidates:
            if c.is_file():
                found.append(Target(c, S_MD_BLOCK, "已存在，将就地追加/替换区块"))
                evidence.append(f"{c}（已存在）")
            else:
                found.append(Target(c, S_MD_BLOCK, "将新建"))
                evidence.append(f"{c}（待建）")
        return DetectResult(bool(found), "；".join(evidence), found)

    def compose(
        self, persona_id: str, body: str, keep_others: bool = False
    ) -> ComposeResult:
        res = ComposeResult()
        for t in self.detect().targets:
            old = t.path.read_text(encoding="utf-8", errors="replace") if t.path.is_file() else ""
            new, replaced = replace_all_blocks(
                old, persona_id, body, insert_at=t.insert_at, keep_others=keep_others
            )
            res.files[t.path] = new
            if replaced:
                res.warnings.append(
                    f"{t.path.name}：该通道原有 {len(replaced)} 份人格"
                    f"（{'、'.join(replaced)}），已替换为 {persona_id}"
                )

            # 位置校验：写进去了 ≠ 会被读到。
            # 宿主读取有限时，超出上限的部分等于不存在——这种失败最隐蔽，
            # 文件里明明有、行为上完全没有，所以必须主动报出来。
            if t.read_limit:
                blk = next((b for b in parse_blocks(new) if b.persona_id == persona_id), None)
                if blk is not None and blk.start > t.read_limit:
                    res.warnings.append(
                        f"{t.path.name}：注入区块在第 {blk.start} 字符，"
                        f"超出宿主读取上限 {t.read_limit} 字符——写进去了但不会生效"
                    )
            res.warnings.extend(conflict_warning(t.path.name, new))
        return res
