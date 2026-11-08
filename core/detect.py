"""注入检测：找出本机所有「被写进客户端里的人格」。

两类来源：

  self        本工具写的（`<!-- persona-hub:begin ... -->`）
  third_party 别的注入工具写的

第三方识别不靠模糊猜测，而是靠**特征签名**。签名全部来自实际取证：

  · 破甲一键通 v8.26 的云记忆区块 —— `<!-- pojia-yijiantong cloudmem v8.18 -->`
    到 `RAW_JSON_END -->`，伴随 `Version: 999999` 与文件只读属性
  · 它的 unlock 标记 `<!-- unlock-v6:h=a92b9e681f84 -->`
  · 它的自证口令「破甲自检」与回执串
  · DSH 侧的 `pojia-guard` 插件条目、`managed-prompts/pojia-yijiantong/` 目录
  · DSH 的 `personaPrefix:` 块标量（人格真正注入的位置）

除此之外还扫两类**结构异常**（低置信，只提示不自动处理）：
  · 目标文件被设成只读，但内容里有人格注入标记
  · 认不出来的托管提示词目录（说明有别的工具在用同一套官方协议）

宁可把「不认识」说成「不认识」，也不把用户的正常内容报成注入。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import all_adapters
from .adapters.base import MARK_BEGIN, MUTED_INLINE_RE, parse_blocks
from .paths import HOME

DSH_HOME = HOME / ".dsh"

# ------------------------------------------------------------------ 特征签名
@dataclass(frozen=True)
class Signature:
    id: str
    name: str
    pattern: re.Pattern
    block_end: re.Pattern | None = None   # 给定时表示这是一个有起止的区块
    severity: str = "warn"
    note: str = ""
    # 能否一键删除。
    # 只对「边界明确」的签名置 True：块有 block_end、或是独立成行的一行注释。
    # 文本特征类（关键词、自证口令）置 False——删掉那个词只会留下一句残句，
    # 那不是清理，是搞脏，而且用户看不出错在哪。
    deletable: bool = False
    toggleable: bool = False


SIGNATURES: tuple[Signature, ...] = (
    Signature(
        id="pojia-cloudmem",
        name="破甲一键通 · 云记忆注入",
        pattern=re.compile(r"<!--\s*pojia-[a-z0-9_]+\s+cloudmem[^>]*-->", re.I),
        block_end=re.compile(r"RAW_JSON_END\s*-->"),
        severity="high",
        note="云记忆区块。破甲一键通把整段人格写进宿主记忆，并抬高版本号防止被覆盖",
        deletable=True,     # 头尾都有明确标记，整块可安全切除
        toggleable=True,
    ),
    Signature(
        id="pojia-unlock",
        name="破甲一键通 · unlock 标记",
        # 先匹配单独成行的 unlock 注释，避免把正文里顺带提到的字样也算进来
        pattern=re.compile(r"^\s*<!--\s*unlock-v\d+:[^>]*-->\s*$", re.M),
        severity="warn",
        note="云记忆区块的附属标记，用于识别人格版本",
        deletable=True,     # 独立成行的注释，删掉不影响其他内容
        toggleable=True,
    ),
    Signature(
        id="pojia-selfcheck",
        name="破甲一键通 · 自证口令",
        pattern=re.compile(r"「破甲自检」|破甲自检"),
        severity="warn",
        note="它靠一句口令验证人格是否被宿主真正载入。属于正文内的关键词，不可单独切除",
        deletable=False,
    ),
    Signature(
        id="pojia-receipt",
        name="破甲一键通 · 引流回执",
        pattern=re.compile(r"自检成功｜项目完全免费|官方\s*QQ\s*交流群"),
        severity="warn",
        note="人格里夹带的群号与推广串。属于正文内的关键词，随整块一起处理",
        deletable=False,
    ),
    Signature(
        id="yaml-persona-prefix",
        name="YAML · personaPrefix 人格字段",
        pattern=re.compile(r"^\s*personaPrefix:\s*[|>]-?\s*$", re.M),
        severity="high",
        note="DSH 真正生效的人格注入位置。破甲一键通在 5 个 node_modules 包里插了这个字段",
        deletable=False,    # 块边界靠缩进界定，自动切除容易切多切少，交给人工或重装包
    ),
    Signature(
        id="pojia-guard",
        name="破甲一键通 · DSH 守卫插件",
        pattern=re.compile(r"pojia-guard"),
        severity="high",
        note="它注册的运行时守卫，会拦截拒答并自动续写",
    ),
)

# 这些关键词一旦出现，说明不是本工具写的、也不是用户自己写的常见内容
GENERIC_TOOL_HINTS = re.compile(
    r"\b(?:injector|persona[-_]?inject|jailbreak[-_]?\w*|破甲|人格注入器)\b", re.I
)


# ------------------------------------------------------------------ 数据结构
@dataclass
class Finding:
    id: str
    kind: str                 # self / third_party / anomaly
    source: str               # 来源标识（persona-hub / pojia-yijiantong / unknown）
    source_name: str
    adapter_id: str
    adapter_name: str
    path: str
    scope: str                # file / dir
    persona_id: str = ""
    state: str = ""           # on / off（仅 self 有）
    line_start: int = 0
    line_end: int = 0
    # 字符偏移：开关/删除靠它精确定位。属于内部坐标，不对外输出。
    off_start: int = 0
    off_end: int = 0
    chars: int = 0
    preview: str = ""
    severity: str = "warn"
    evidence: str = ""
    toggleable: bool = True
    deletable: bool = True
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "source": self.source,
            "source_name": self.source_name,
            "adapter_id": self.adapter_id,
            "adapter_name": self.adapter_name,
            "path": self.path,
            "scope": self.scope,
            "persona_id": self.persona_id,
            "state": self.state,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "chars": self.chars,
            "preview": self.preview,
            "severity": self.severity,
            "evidence": self.evidence,
            "toggleable": self.toggleable,
            "deletable": self.deletable,
            "extra": self.extra,
        }


def _make_id(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:16]


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _preview(text: str, limit: int = 260) -> str:
    s = text.strip()
    return s if len(s) <= limit else s[:limit] + " …"


# ------------------------------------------------------------------ 文件级扫描
def scan_text(
    adapter_id: str,
    adapter_name: str,
    path: Path,
    text: str,
) -> list[Finding]:
    """扫描单个目标文件，返回其中的注入痕迹。"""
    out: list[Finding] = []

    # ---- 1) 本工具自己的区块
    for blk in parse_blocks(text):
        out.append(
            Finding(
                id=_make_id("self", str(path), blk.persona_id, blk.hash),
                kind="self",
                source="persona-hub",
                source_name="人格中枢",
                adapter_id=adapter_id,
                adapter_name=adapter_name,
                path=str(path),
                scope="file",
                persona_id=blk.persona_id,
                state=blk.state,
                line_start=_line_of(text, blk.start),
                line_end=_line_of(text, blk.end),
                off_start=blk.start,
                off_end=blk.end,
                chars=len(blk.text),
                preview=_preview(blk.body),
                severity="info",
                evidence=f"标记 persona-hub:begin id={blk.persona_id} state={blk.state}",
            )
        )

    # ---- 2) 已知第三方签名（同一签名在同一文件内的多处命中合并为一项）
    for sig in SIGNATURES:
        hits = list(sig.pattern.finditer(text))
        if not hits:
            continue

        first = hits[0]
        last_pos = first.end()
        end_text = ""

        if sig.block_end is not None:
            m_end = sig.block_end.search(text, first.end())
            if m_end:
                last_pos = m_end.end()
                end_text = text[first.start() : m_end.end()]

        start_line = _line_of(text, first.start())
        end_line = _line_of(text, max(last_pos - 1, first.start()))

        out.append(
            Finding(
                id=_make_id("third", str(path), sig.id),
                kind="third_party",
                source="pojia-yijiantong" if sig.id.startswith("pojia") else "unknown",
                source_name="破甲一键通" if sig.id.startswith("pojia") else "未知注入工具",
                adapter_id=adapter_id,
                adapter_name=adapter_name,
                path=str(path),
                scope="file",
                line_start=start_line,
                line_end=end_line,
                off_start=first.start(),
                off_end=max(last_pos, first.end()),
                chars=len(end_text) if end_text else sum(len(h.group(0)) for h in hits),
                preview=_preview(end_text or first.group(0)),
                severity=sig.severity,
                evidence=f"{sig.name}（命中 {len(hits)} 处）｜{sig.note}",
                toggleable=sig.toggleable,
                deletable=sig.deletable,
                extra={"signature": sig.id, "hits": len(hits)},
            )
        )

    # ---- 3) 被本工具停用的第三方注入（能停也要能开，否则只是单向删除）
    for m in MUTED_INLINE_RE.finditer(text):
        src = m.group(1)
        out.append(
            Finding(
                id=_make_id("muted", str(path), src, m.group(2)),
                kind="third_party",
                source=src,
                source_name="破甲一键通" if "pojia" in src.lower() else f"未知工具（{src}）",
                adapter_id=adapter_id,
                adapter_name=adapter_name,
                path=str(path),
                scope="file",
                state="off",
                line_start=_line_of(text, m.start()),
                line_end=_line_of(text, m.end()),
                off_start=m.start(),
                off_end=m.end(),
                chars=0,
                preview=f"已停用：原 {m.group(3)} 行内容保存在 data/muted/{m.group(2)}.txt",
                severity="info",
                evidence="由人格中枢停用，可随时重新启用",
            )
        )

    # ---- 4) 结构异常：文件被设成只读，但里面有人格注入
    has_injection = MARK_BEGIN in text or any(s.pattern.search(text) for s in SIGNATURES)
    if has_injection and path.is_file():
        try:
            import stat as _stat

            mode = path.stat().st_mode
            if not (mode & _stat.S_IWRITE):
                out.append(
                    Finding(
                        id=_make_id("ro", str(path)),
                        kind="anomaly",
                        source="unknown",
                        source_name="权限异常",
                        adapter_id=adapter_id,
                        adapter_name=adapter_name,
                        path=str(path),
                        scope="file",
                        line_start=1,
                        line_end=1,
                        severity="warn",
                        preview="该文件被设为只读，但内容里存在人格注入",
                        evidence="注入类工具常把目标文件设成只读防止被覆盖，副作用是你自己也改不动它",
                        toggleable=False,
                        deletable=False,
                        extra={"kind": "readonly"},
                    )
                )
        except OSError:
            pass

    return out


# ------------------------------------------------------------------ 目录级扫描
def scan_managed_prompts() -> list[Finding]:
    """DSH 的官方托管提示词目录：一个子目录 = 一个注入工具。"""
    out: list[Finding] = []
    base = DSH_HOME / "managed-prompts"
    if not base.is_dir():
        return out

    for d in sorted(p for p in base.iterdir() if p.is_dir()):
        name = d.name
        is_self = name == "persona-hub"
        is_pojia = "pojia" in name.lower()

        files = [f for f in d.rglob("*") if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        sample = ""
        for f in files:
            if f.suffix in (".md", ".txt", ".json"):
                try:
                    sample = f.read_text(encoding="utf-8", errors="replace")[:400]
                    break
                except OSError:
                    continue

        out.append(
            Finding(
                id=_make_id("mp", str(d)),
                kind="self" if is_self else "third_party",
                source="persona-hub" if is_self else ("pojia-yijiantong" if is_pojia else "unknown"),
                source_name="人格中枢" if is_self else ("破甲一键通" if is_pojia else f"未知工具（{name}）"),
                adapter_id="dsh",
                adapter_name="DSH（DeepSeek Harness）",
                path=str(d),
                scope="dir",
                line_start=0,
                line_end=0,
                chars=size,
                preview=_preview(sample) or f"{len(files)} 个文件",
                severity="info" if is_self else "high",
                evidence=f"托管提示词目录 managed-prompts/{name}（{len(files)} 个文件，{size} 字节）",
                toggleable=False,   # 目录型不提供开关，只能整体移除
                deletable=not is_self,
                extra={"files": len(files), "dir_name": name},
            )
        )
    return out


def scan_dsh_profile() -> list[Finding]:
    """DSH profile 里被追加的插件条目与依赖。"""
    out: list[Finding] = []
    profiles = DSH_HOME / "profiles"
    if not profiles.is_dir():
        return out

    for prof in sorted(p for p in profiles.iterdir() if p.is_dir()):
        patch = prof / "cordis.patch.yml"
        if patch.is_file():
            text = patch.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r"^\s*-\s*insert:\s*$", text, re.M):
                block = text[m.start() : m.start() + 600]
                for sig in SIGNATURES:
                    if sig.pattern.search(block):
                        out.append(
                            Finding(
                                id=_make_id("patch", str(patch), sig.id),
                                kind="third_party",
                                source="pojia-yijiantong",
                                source_name="破甲一键通",
                                adapter_id="dsh",
                                adapter_name="DSH（DeepSeek Harness）",
                                path=str(patch),
                                scope="file",
                                line_start=_line_of(text, m.start()),
                                line_end=_line_of(text, m.start() + len(block)),
                                chars=len(block),
                                preview=_preview(block),
                                severity="high",
                                evidence=(
                                    f"profile 的 cordis.patch.yml 里插入了 {sig.name}｜"
                                    "YAML 结构型条目只能移除、不能停用（停用会让 YAML 解析失败）"
                                ),
                                toggleable=False,
                                extra={"signature": sig.id},
                            )
                        )
                        break

        pkg = prof / "package.json"
        if pkg.is_file():
            try:
                import json

                data = json.loads(pkg.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            deps = data.get("dependencies") or {}
            for name in deps:
                if "pojia" in name.lower():
                    out.append(
                        Finding(
                            id=_make_id("dep", str(pkg), name),
                            kind="third_party",
                            source="pojia-yijiantong",
                            source_name="破甲一键通",
                            adapter_id="dsh",
                            adapter_name="DSH（DeepSeek Harness）",
                            path=str(pkg),
                            scope="file",
                            line_start=0,
                            line_end=0,
                            preview=f'"{name}": "{deps[name]}"',
                            severity="high",
                            evidence=(
                                "profile 的 package.json 里注册了该工具的插件依赖｜"
                                "JSON 结构型条目只能移除、不能停用（停用会让 JSON 解析失败）"
                            ),
                            toggleable=False,
                            extra={"dep": name},
                        )
                    )
    return out


# ------------------------------------------------------------------ WorkBuddy 云记忆
WB_MEMORY = HOME / ".workbuddy" / "memory"


def scan_workbuddy_cloud() -> list[Finding]:
    """扫 WorkBuddy 的云记忆层。

    这一层不在任何适配器的注入目标里（它由宿主自己维护），
    但恰恰是破甲一键通真正写人格的地方——漏了它，检测就形同虚设。
    """
    out: list[Finding] = []
    if not WB_MEMORY.is_dir():
        return out

    for f in sorted(WB_MEMORY.glob("*_memory.md")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue

        hits = scan_text("workbuddy_cloud", "WorkBuddy（云记忆层）", f, text)
        for h in hits:
            # 云记忆是宿主自己维护并会重写的：删了可能被重新拉回，停用也可能被覆盖
            h.evidence += "｜注意：此文件由宿主维护，改动可能在下次同步时被覆盖"
        out.extend(hits)

        # 版本号被抬高也是痕迹（防宿主回退覆盖）
        if re.search(r"^>\s*Version:\s*9{6,}\s*$", text, re.M) and not any(
            h.kind == "third_party" for h in hits
        ):
            out.append(
                Finding(
                    id=_make_id("ver", str(f)),
                    kind="anomaly",
                    source="unknown",
                    source_name="版本号异常",
                    adapter_id="workbuddy_cloud",
                    adapter_name="WorkBuddy（云记忆层）",
                    path=str(f),
                    scope="file",
                    line_start=1,
                    line_end=1,
                    preview="记忆头部的 Version 被写成 999999",
                    severity="warn",
                    evidence="抬高版本号是为了让宿主无法用旧版本回退覆盖，属于注入工具的常见手法",
                    toggleable=False,
                    deletable=False,
                )
            )
    return out


# ------------------------------------------------------------------ 汇总
CHANNEL_LABELS = {
    "app-config.json": "自定义指令",
}


def _channel_label(f: Finding) -> str:
    """一条痕迹落在哪条通道上，用用户看得懂的名字。"""
    name = Path(f.path).name
    if f.scope == "dir":
        return f.extra.get("dir_name") or name
    return CHANNEL_LABELS.get(name, name)


def annotate_groups(findings: list[Finding]) -> None:
    """同一个人格写进多条通道时，打上同一个 group 标记。

    为什么必须合：宿主按会话类型二选一注入，所以**一个人格本来就写两处**
    （自定义指令 + USER.md）。不合成的话，「注入管理」里会出现两行几乎一样的人格，
    用户的第一反应是「怎么注入了两个」——而实际上只有一个，只是走了两条路。

    只在同一个人格命中 ≥2 处时标注；单通道的不加标记，前端按原样渲染。
    """
    buckets: dict[tuple[str, str], list[Finding]] = {}
    for f in findings:
        if f.kind != "self" or not f.persona_id:
            continue
        buckets.setdefault((f.adapter_id, f.persona_id), []).append(f)

    for (adapter_id, persona_id), group in buckets.items():
        if len(group) < 2:
            continue
        labels = [_channel_label(f) for f in group]
        for f in group:
            f.extra["group"] = f"{adapter_id}:{persona_id}"
            f.extra["group_size"] = len(group)
            f.extra["group_channels"] = labels
            f.extra["channel"] = _channel_label(f)


def scan_all() -> list[Finding]:
    """全量扫描。返回按严重度排序的清单。"""
    findings: list[Finding] = []

    for adapter in all_adapters():
        det = adapter.detect()
        if not det.detected:
            continue
        for t in det.targets:
            if not t.path.is_file():
                continue
            try:
                text = t.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            findings.extend(scan_text(adapter.id, adapter.name, t.path, text))

    findings.extend(scan_managed_prompts())
    findings.extend(scan_dsh_profile())
    findings.extend(scan_workbuddy_cloud())

    annotate_groups(findings)

    order = {"high": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.adapter_id, f.path))
    return findings


def summarize(findings: list[Finding]) -> dict:
    third = [f for f in findings if f.kind == "third_party"]
    own = [f for f in findings if f.kind == "self"]
    return {
        "total": len(findings),
        "self": len(own),
        "self_on": sum(1 for f in own if f.state == "on"),
        "self_off": sum(1 for f in own if f.state == "off"),
        "third_party": len(third),
        "high": sum(1 for f in findings if f.severity == "high"),
        "sources": sorted({f.source_name for f in third}),
    }
