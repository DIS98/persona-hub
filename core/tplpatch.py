"""给宿主身份模板补 SOUL 插槽：让专家会话也能读到人格。

为什么需要补（2026-10-31 源码 + 模板双向核实）
================================================
`app.asar` 里身份注入的原文是：

    c = [ readFileIfExists(SOUL.md), s ? read(...BOOTSTRAP) : '', read(...USER.md) ];
    t.isExpert || c.push(readFileIfExists(IDENTITY.md));
    e.SoulContent = truncateContent(l[0], Ls);   // 专家会话也读了 SOUL

**数据层无条件读 SOUL.md**，但两个模板的插槽不一样：

    普通会话 user-context-identity.tpl       : SOUL + IDENTITY + USER + toneStyle + customPrompt
    专家会话 user-context-expert-identity.tpl : 只有 USER

所以专家会话里 SOUL 的内容虽然被读进来，却没有插槽可渲染，等于不存在。
后果很具体：人格写 USER.md 就会和专家团 prompt 抢解释权（实测漂移：两套输出格式
各取一半、团队机制被省掉）；人格写 SOUL.md 则专家会话根本读不到。

补一行 `{{ SoulContent }}` 之后：人格放 SOUL.md，普通会话与专家会话都生效，
USER.md 彻底还给"用户画像"，两边不再抢同一个文件。

状态只有三档，界面与预检都按这三档走
====================================
    absent    宿主里没有这个模板（路径不同或版本差异）
    patched   已含 SoulContent 插槽
    missing   模板在，但插槽没了 —— 十有八九是 WorkBuddy 升级覆盖

`missing` 必须能被看见。升级不会报错、不会提示，只会在某天"人格怎么不生效了"
的时候才发现，而那时已经错过好几轮对话。
"""
from __future__ import annotations

import hashlib
import os
import json
import re
import time
from pathlib import Path

from .paths import data_root, host_install_dir

EXPERT_TPL = "user-context-expert-identity.tpl"
IDENTITY_TPL = "user-context-identity.tpl"

# 插进 identity_context 里的那三行。列成常量是因为测试要按它断言行数与内容。
SOUL_SLOT = [
    "## SOUL.md",
    "Path: {{ SoulPath }}",
    "{% if SoulContent %}{{ SoulContent | trim }}{% else %}(empty or missing){% endif %}",
    "",
]

# 光有内容不够，还要有"授权句"。普通模板里这句是 `If SOUL.md is present, embody its
# persona and tone.` —— 没有它，模型只会把 SOUL 当成背景资料，不会当人格去演。
PERSONA_LINE = "If SOUL.md is present, embody its persona and tone."
# 判"有没有插槽"必须用带词边界的正则，不能用 `"{{ SoulContent" in text`。
# 子串判定会被 `{{ SoulContentX }}` 这种"改错名字"的模板骗过去 ——
# 那种模板渲染时变量不存在，等于没补，而界面会一直显示"已补"。
SOUL_SLOT_RE = re.compile(r"\{\{\s*SoulContent\b")
ANCHOR_USER = "## USER.md"
ANCHOR_CONSISTENT = "Stay consistent with the latest injected user profile."
ANCHOR_END = "</identity_context>"

# 契约尾块：人格设定在身份段的**最后一份**文本。
# 为什么必须放最后 —— 位置决定权重，不是修辞。
# 身份段后面还接着专家团 prompt（本机实测 31,461 字节 /SOUL.md 9,098 字节），
# 人格本体因此落在整段上下文的腰部。Liu et al. 2023（Lost in the Middle，
# arXiv 2307.03172）实测中段信息准确率相对头尾下降 30% 以上；Chroma 2025 对 18 个
# 模型复测，全部受影响，无一消除。所以不重写人格正文，只把"三个裁决"复述到段末 ——
# 增量 687 字节，换回落点的注意力权重。
CONTRACT_MARK = "<!-- persona-hub:contract:v1 -->"
CONTRACT_TAIL = [
    CONTRACT_MARK,
    "## 本会话的输出契约",
    "",
    "以下三条对本次会话每一次回复生效，与上文身份文件属同一份设定。",
    "",
    "1. **格式服从人格**：末行标识、开口、收尾、禁用词，按人格文件执行。",
    "2. **事实服从专家**：专家团在场时，角色分工、判据、平台事实与验收标准以专家团为准；"
    "人格只接管输出形式，不接管专业判断。",
    "3. **边界不外扩**：本契约只约束输出形式与协作分工，不改写宿主的权限声明、"
    "安全检查与内容边界。",
    "",
    "冲突时按 1 → 2 → 3 裁决：形式人格优先，判断专家优先，其余保持宿主原样。",
    "",
]


class TplError(RuntimeError):
    pass


def _templates_dir() -> Path:
    return host_install_dir() / "resources" / "app.asar.unpacked" / "resources" / "templates"


def _tpl(name: str) -> Path:
    return _templates_dir() / name


def _sha16(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _backup_dir() -> Path:
    p = data_root() / "tpl-backup"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _record_path() -> Path:
    return data_root() / "tpl-patch.json"


def _host_record_path() -> Path:
    """宿主模板目录里再放一份记录。

    为什么双写：备份与记录默认落在 persona-hub 的 data 目录，而 data 目录会被
    `PERSONA_HUB_HOME` 改掉（测试隔离）、也可能因为 exe 换目录而变。
    一旦变了，revert 就找不到备份，"补过又不知道原文件在哪"是最难受的状态。
    宿主这边的记录只在补丁成功时写一次，指向备份的绝对路径。
    """
    return _templates_dir() / ".persona-hub-patch.json"


def _read_record() -> dict:
    for p in (_record_path(), _host_record_path()):
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
        except (json.JSONDecodeError, OSError):
            continue
    return {}


def _write_record(data: dict) -> None:
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    for p in (_record_path(), _host_record_path()):
        try:
            p.write_text(body, encoding="utf-8", newline="\n")
        except OSError:
            # 数据目录那份必须成功；宿主那份失败不致命（权限/只读盘），不因此让补丁算失败
            pass


def host_version() -> dict:
    """宿主构建标识。升级检测的锚点。

    用 `install-manifest.json` 而不是 app.asar 里的 package.json：
    前者在 resources 根、是明文、且每次安装都会重写 `generatedAt`，
    asar 里那份要解析包格式才能读，为一行版本号不值得。
    """
    mf = host_install_dir() / "resources" / "install-manifest.json"
    out = {"appVersion": "", "generatedAt": ""}
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
        out["appVersion"] = str(data.get("appVersion") or "")
        out["generatedAt"] = str(data.get("generatedAt") or "")
    except (OSError, json.JSONDecodeError):
        pass
    return out


def analyze_text(name: str, text: str, size: int = 0, sha: str = "") -> dict:
    """按模板文本判状态。抽成纯函数是为了能对任意文本断言——

    判据只能对着真实宿主文件跑，就没法造"插槽被删掉"的样本，
    也就没法证明这条判据真的能报红。
    """
    item = {
        "name": name,
        "path": str(_tpl(name)),
        "exists": True,
        "has_soul_slot": bool(SOUL_SLOT_RE.search(text)),
        "has_persona_line": PERSONA_LINE in text,
        "has_contract": CONTRACT_MARK in text,
        "size": size,
        "sha16": sha,
        "state": "",
    }
    if item["has_soul_slot"]:
        item["state"] = "patched"
    else:
        item["state"] = "missing" if name == EXPERT_TPL else "absent"
    return item


def _analyze(name: str) -> dict:
    p = _tpl(name)
    if not p.is_file():
        return {
            "name": name,
            "path": str(p),
            "exists": False,
            "has_soul_slot": False,
            "has_persona_line": False,
            "size": 0,
            "sha16": "",
            "state": "absent",
        }
    raw = p.read_bytes()
    return analyze_text(name, raw.decode("utf-8", "replace"), len(raw), _sha16(raw))


def status() -> dict:
    """只读检查。界面、预检、`/api/doctor` 都走这里。"""
    host = host_version()
    rec = _read_record()
    tpls = [_analyze(EXPERT_TPL), _analyze(IDENTITY_TPL)]
    expert = next(t for t in tpls if t["name"] == EXPERT_TPL)

    warnings: list[str] = []
    stale = False
    if rec:
        rec_ver = str(rec.get("appVersion") or "")
        if host["appVersion"] and rec_ver and rec_ver != host["appVersion"]:
            stale = True
            warnings.append(
                f"WorkBuddy 已从 {rec_ver} 升级到 {host['appVersion']}，模板可能被新版覆盖。"
                + ("" if expert["has_soul_slot"] else "实测插槽已消失——人格在专家会话里不生效，需要重新补。")
            )
        elif not expert["has_soul_slot"]:
            warnings.append("补丁记录还在，但插槽已经没了——模板被覆盖或手工改回，需要重新补。")
    elif expert["exists"] and not expert["has_soul_slot"]:
        warnings.append(
            "专家模板没有 SOUL 插槽：专家会话只读 USER.md。人格若写在 USER.md，会和专家团 prompt 抢解释权。"
        )
    if expert["exists"] and expert["has_soul_slot"] and not expert["has_contract"]:
        warnings.append(
            "插槽在，但没有契约尾块：人格会被专家团 prompt 挤到中段，格式与分工容易各取一半。重新补一次即可。"
        )

    return {
        "host": host,
        "templates": tpls,
        "expert": expert,
        "record": rec,
        "stale": stale,
        "warnings": warnings,
    }


def _check_rendered(text: str) -> None:
    """补完自检。写坏了比不补更糟——模板坏了宿主直接没有身份段。"""
    # 必须用正则数，不能 count("{% if")：
    # Jinja 的空白控制写法 `{%- endif %}` 不含子串 `{% endif`，用 count 会少数一个，
    # 于是原文本身就被判成"不配对"（实测第一次跑就栽在这里）。
    n_if = len(re.findall(r"\{%-?\s*if\b", text))
    n_endif = len(re.findall(r"\{%-?\s*endif\b", text))
    if n_if != n_endif:
        raise TplError(f"Jinja 块不配对：if × {n_if} / endif × {n_endif}")
    if not SOUL_SLOT_RE.search(text):
        raise TplError("插入后仍找不到 SoulContent 插槽")
    if CONTRACT_MARK not in text:
        raise TplError("插入后仍找不到契约尾块")
    for need in ("<identity_context>", "</identity_context>", "{{ UserPath }}"):
        if need not in text:
            raise TplError(f"关键结构丢失：{need}")


def _atomic_write(target: Path, data: bytes) -> None:
    """同目录临时文件 + os.replace。同目录是必须的：跨盘 replace 不是原子操作。

    为什么值得单独写一个：宿主模板写坏了不会有任何报错，只会在下一次会话里
    悄悄少一段身份。这种故障一旦发生，排查成本远高于这里多写十行。
    """
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    try:
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def patch() -> dict:
    """补插槽。幂等：已经补过直接返回，不改文件。"""
    p = _tpl(EXPERT_TPL)
    if not p.is_file():
        raise TplError(f"找不到专家模板：{p}")

    raw = p.read_bytes()
    # 严格解码：解不开就说明模板不是 UTF-8（换了编码或被别的工具改过）。
    # 用 replace 硬解会得到一堆 U+FFFD，写回去就是把模板彻底毁掉。
    try:
        original = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TplError(f"模板不是合法的 UTF-8（{exc}），已中止，文件未改动") from exc
    text = original

    if PERSONA_LINE not in text:
        if ANCHOR_CONSISTENT not in text:
            raise TplError("模板结构与预期不同（找不到 'Stay consistent...' 锚点），已中止")
        text = text.replace(ANCHOR_CONSISTENT, PERSONA_LINE + "\r\n" + ANCHOR_CONSISTENT, 1)

    if not SOUL_SLOT_RE.search(text):
        if ANCHOR_USER not in text:
            raise TplError("模板结构与预期不同（找不到 '## USER.md' 锚点），已中止")
        slot = "\r\n".join(SOUL_SLOT) + "\r\n"
        text = text.replace(ANCHOR_USER, slot + ANCHOR_USER, 1)

    if CONTRACT_MARK not in text:
        if ANCHOR_END not in text:
            raise TplError("模板结构与预期不同（找不到 '</identity_context>' 锚点），已中止")
        tail = "\r\n".join(CONTRACT_TAIL) + "\r\n"
        text = text.replace(ANCHOR_END, tail + ANCHOR_END, 1)

    if text == original:
        return {"patched": False, "reason": "已经是补好的状态", "path": str(p)}
    _check_rendered(text)

    # 先备份再写。备份名带原文件 sha，升级换了版本时不会覆盖旧备份。
    # 但**补丁自己升级**时（v1 只补插槽、v2 加契约尾块）不能拿"已打过补丁的版本"当原档，
    # 否则 revert 还原回来的是半成品。所以记录里已有备份就沿用它，不覆盖。
    prev = _read_record()
    bak = None
    if prev.get("backup"):
        cand = Path(str(prev["backup"]))
        if cand.is_file():
            bak = cand
    if bak is None:
        bak = _backup_dir() / f"{EXPERT_TPL}.{_sha16(raw)}.orig"
        if not bak.exists():
            bak.write_bytes(raw)

    # 原子写：先落同目录临时文件再 os.replace。直接 write_bytes 写到一半断电/被杀，
    # 宿主拿到的就是一个截断的模板——整段身份不渲染，且没有任何报错。
    try:
        _atomic_write(p, text.encode("utf-8"))     # 二进制写：文本模式会动 CRLF
    except OSError as exc:
        raise TplError(f"写入宿主模板失败：{exc}") from exc

    # 写完回读比对。Windows 上遇到过杀软占用导致写入被截断的情况，
    # 不回读就当成功，等于把一个坏模板留在宿主里。
    back = p.read_bytes().decode("utf-8", "replace")
    if not SOUL_SLOT_RE.search(back):
        _atomic_write(p, raw)
        raise TplError("写入后回读不到插槽，已还原原文件")

    host = host_version()
    _write_record({
        "appVersion": host["appVersion"],
        "generatedAt": host["generatedAt"],
        "patchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "template": EXPERT_TPL,
        "originalSha16": _sha16(raw),
        "patchedSha16": _sha16(p.read_bytes()),
        "backup": str(bak),
    })
    return {
        "patched": True,
        "path": str(p),
        "backup": str(bak),
        "note": "需要完全退出 WorkBuddy 再打开，新模板才会被读入。",
    }


def revalidate() -> dict:
    """文件是好的、但版本号对不上时，把记录里的版本号刷成当前宿主版本。

    为什么需要：升级不一定覆盖模板（小版本更新、只换资源包都会保留），
    这时 `stale` 会永远为真 —— 界面天天报"可能被覆盖"，自愈任务也会空转。
    判据只看**文件本身对不对**，不看版本号对不对。
    """
    p = _tpl(EXPERT_TPL)
    if not p.is_file():
        return {"ok": False, "reason": "模板不存在"}
    raw = p.read_bytes()
    text = raw.decode("utf-8", "replace")
    if not SOUL_SLOT_RE.search(text):
        return {"ok": False, "reason": "插槽不在"}
    if CONTRACT_MARK not in text:
        return {"ok": False, "reason": "契约尾块不在"}
    host = host_version()
    rec = _read_record()
    rec.update({
        "appVersion": host["appVersion"],
        "generatedAt": host["generatedAt"],
        "revalidatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "patchedSha16": _sha16(raw),
    })
    _write_record(rec)
    return {"ok": True, "appVersion": host["appVersion"]}


def revert() -> dict:
    """还原成原版。优先用记录里的备份，其次找同目录最新的 .orig。"""
    rec = _read_record()
    cand: Path | None = None
    if rec.get("backup"):
        b = Path(str(rec["backup"]))
        if b.is_file():
            cand = b
    if cand is None:
        files = sorted(_backup_dir().glob(f"{EXPERT_TPL}.*.orig"),
                       key=lambda x: x.stat().st_mtime, reverse=True)
        cand = files[0] if files else None
    if cand is None:
        raise TplError("没有可用的原版备份，无法还原")

    p = _tpl(EXPERT_TPL)
    try:
        _atomic_write(p, cand.read_bytes())
    except OSError as exc:
        raise TplError(f"还原写入失败：{exc}") from exc
    _write_record({})
    return {"reverted": True, "from": str(cand), "path": str(p)}
