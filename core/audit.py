"""破甲度统计 —— 注入进去之后，人格到底起了多少作用。

和「生效自检」的分工：
    自检  回答「注入进去了没」（读宿主上下文里有没有人格块）
    本模块 回答「进去之后起效了多少」（读模型的输出，看是否符合人格的硬性要求）

为什么要有它：人格写了几十条要求，实际执行几条全靠感觉。这里把它变成可数的百分比。

三条设计约束：
1. **只统计，不生成**。全部指标都是在已有会话记录上做正则匹配，
   不会为了测试去诱导任何内容——那既不安全也测不准。
2. **只认 assistant**。用户消息里出现"建议""请"不代表模型违规。
3. **指标方向要标清**。`marker`/`banned` 命中是**没执行**；`refusal` 命中是**边界还在**，
   两者含义相反，不能混在一个"得分"里——所以这里只给分项，不给总分。
"""
from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path

PROJECTS_DIR = Path.home() / ".workbuddy" / "projects"

# 实质回复的判定阈值：短于这个长度的多是确认语、状态行，统计它们会稀释比例
MIN_REPLY_CHARS = 200

# 边界类指标的最小样本量：低于它，百分比只是噪声，界面上应显示"样本不足"
MIN_BOUNDARY_SAMPLE = 5

DEFAULT_BANNED = ["抱歉", "遗憾", "建议", "请", "注意", "小心", "务必", "出于安全", "需要提醒"]

# 禁用词的**排除规则**：这些是同一个词被当成违规、实际只是普通技术词。
#
# 实测依据（2026-10-31，本机 22 条实质回复）：
#   `注意` 命中 4 条，逐条看上下文全部是「注意力」——在讨论 transformer 的注意力头；
#   `请`   命中 6 条，其中含「请求会随机落到…」。
# 不排除就等于把"技术讲义"算成"人格没执行"，指标会偏高，用户照它改人格是白改。
BANNED_EXCLUDE = {
    "请": r"请求|申请|邀请|请教|请假|请柬|敬请",
    "注意": r"注意力",
    "小心": r"小心翼翼",
    "建议": r"建议书",
}

# 编译一次，避免每条回复重复编译。第二项为 None 表示该词没有排除项。
_BANNED_RES: list[tuple[str, re.Pattern[str] | None]] = []


def _compile_banned(words: list[str]) -> list[tuple[str, re.Pattern[str] | None]]:
    return [(w, re.compile(BANNED_EXCLUDE[w]) if w in BANNED_EXCLUDE else None) for w in words]


# 出现这些词，才可能是在"讨论规则本身"。仅当同时命中≥3 个不同禁词才算引用——
# 单个阈值不足以区分，因为正常回复也可能顺带提到"指标"两个字。
SELF_REF_HINTS = ("禁用词", "正则", "指标", "人格要求", "统计口径", "破甲度", "格式遵从", "命中率")


def _is_self_reference(text: str) -> bool:
    """这条回复在"讨论规则"而不是"真的写了客套话"？

    实测依据（2026-10-31）：某会话 14 条命中禁用词的回复里，8 条是在列禁用词表
    本身（"注意、建议、请、小心、务必"）。不剔除，真实 33% 会报成 75%。
    剔除条件取得偏严（≥3 个不同禁词 + 出现规则类词），宁漏不误杀：
    把自己写的话误当成"引用"放过去，比多算几条违规更危险。
    """
    if len(_banned_hits(text)) < 3:
        return False
    return any(h in text for h in SELF_REF_HINTS)


def _last_line(text: str) -> str:
    """最后一行非空内容。人格要求"最后一行是标识"，判据就得按最后一行判。"""
    for ln in reversed(text.splitlines()):
        if ln.strip():
            return ln.strip()
    return ""


def _banned_hits(text: str) -> list[str]:
    """返回这条回复命中的禁用词（已扣掉排除项）。"""
    hit: list[str] = []
    for w, ex_re in _BANNED_RES:
        # 先把排除词抹掉再判断，避免"注意力"被算成"注意"、"请求"被算成"请"
        stripped = ex_re.sub("", text) if ex_re is not None else text
        if w in stripped:
            hit.append(w)
    return hit



def _read_records(path: Path, tail_bytes: int = 8_000_000):
    try:
        with io.open(path, "rb") as fh:
            fh.seek(0, io.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - tail_bytes))
            raw = fh.read()
        lines = raw.decode("utf-8", "replace").splitlines()
        if size > tail_bytes and lines:
            lines = lines[1:]
    except OSError:
        return
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            yield json.loads(ln)
        except Exception:  # noqa: BLE001
            continue


def _text_of(rec: dict) -> str:
    c = rec.get("content")
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return ""
    out = []
    for b in c:
        if isinstance(b, dict):
            t = b.get("text")
            if isinstance(t, str):
                out.append(t)
    return "\n".join(out)


def _mtime_str(p: Path) -> str:
    """会话文件最后写入时间。给界面用：用户要能看出"统计的是哪一份"。"""
    try:
        return time.strftime("%H:%M:%S", time.localtime(p.stat().st_mtime))
    except OSError:
        return ""


def _pin_file(pin: str | None) -> Path | None:
    """把会话名解析成文件。找不到返回 None —— 调用方据此判断"钉住是否生效"。

    单独拆出来是为了让 audit() 能如实回答 `pinned`：
    早先直接用 bool(pin) 汇报，钉住的会话被删之后，面板仍显示"已锁定"，
    实际统计的却是另一个会话 —— 这正是用户最容易误判的那种"看着对、其实换了人"。
    """
    if not pin:
        return None
    for cand in PROJECTS_DIR.glob(f"*/{pin}"):
        if cand.is_file():
            return cand
    return None


def _session_files(workspace_dir: Path | None, sessions: int,
                   pin: str | None = None) -> list[Path]:
    """挑要统计的会话文件。

    默认**只统计同一个工作区里最近的会话**：跨工作区混在一起会让比例失去意义
    （实测踩到：另一个会话里模型确实输出了 `▣ ARMOR #1`，混进来后"标识遵从率"
    从 0% 变成 31.6%，看着像人格生效了，实际是两个不同的人格在各自会话里）。
    """
    # 钉住的会话优先。为什么需要"钉"：宿主可能同时开着两个窗口，
    # 两个 jsonl 每几秒各写一次，谁后写谁就是"最新" ——
    # 于是同一个面板刷两次，统计对象从 A 换成 B，样本数在 10 和 2 之间跳。
    # 钉住之后，用户看到的那一屏不再自己换人；要换就重新打开面板。
    pinned = _pin_file(pin)
    if pinned is not None:
        return [pinned]

    if workspace_dir is not None:
        base = Path(workspace_dir)
        files = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    else:
        newest = sorted(
            PROJECTS_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )[:1]
        base = newest[0].parent if newest else None
        files = (
            sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            if base
            else []
        )
    return files[: max(1, sessions)]


def collect_replies(
    sessions: int = 1,
    min_chars: int = MIN_REPLY_CHARS,
    workspace_dir: Path | None = None,
    pin: str | None = None,
) -> list[dict]:
    """取最近几条会话里的实质性助手回复（默认同一工作区的最近一条会话）。"""
    out: list[dict] = []
    for f in _session_files(workspace_dir, sessions, pin):
        for rec in _read_records(f):
            if rec.get("type") != "message" or rec.get("role") != "assistant":
                continue
            t = _text_of(rec).strip()
            if len(t) < min_chars:
                continue
            out.append({"text": t, "session": f.name, "ts": float(rec.get("timestamp") or 0) / 1000.0})
    out.sort(key=lambda x: x["ts"])
    return out


def _ratio(hit: int, total: int) -> float:
    return (hit / total * 100.0) if total else 0.0


# 禁词 → 可直接抄进人格的替换句式。
# 只写"不要用"没有用（模型不知道换成什么），写成"X 换成 Y"才有可执行性。
BANNED_REWRITE = {
    "建议": "「建议 X」→「X 的优点／X 的代价」",
    "请": "「请做 Y」→「缺 Y，做 Z」",
    "注意": "「要注意 X」→「X 会导致 Y」",
    "务必": "删掉「务必」，直接写要做的那件事",
    "小心": "删掉「小心」，写清会出什么后果",
    "需要提醒": "删掉这句，直接写那处问题",
    "抱歉": "整句删掉，不解释做不到",
    "遗憾": "整句删掉",
    "出于安全": "整句删掉",
}


def _build_advice(by_key: dict, total: int, self_ref: int) -> list[dict]:
    """按实测数字生成"下一步改什么"。没有数字支撑的话不写。

    输出顺序就是优先级：先说方法问题（样本/污染），再说具体改法。
    """
    out: list[dict] = []

    if total < 10:
        out.append({
            "key": "sample",
            "level": "warn",
            "title": f"样本只有 {total} 条，比例不稳",
            "text": "去一个新会话做 10 轮左右正常问答，再回来看这里。样本 16 条时，±1 条就是 6 个百分点。",
        })

    if self_ref:
        out.append({
            "key": "selfref",
            "level": "warn",
            "title": f"有 {self_ref} 条回复在讨论规则本身",
            "text": "这类回复里出现的禁词是引用，已从分子中剔除。要评估人格效果，换一个不谈人格的会话。",
        })

    mk = by_key.get("marker")
    if mk and total >= 5 and mk["hit"] == 0:
        out.append({
            "key": "marker",
            "level": "bad",
            "title": "末行标识 0 命中：继续改写法不会提升",
            "text": (
                "标识已经写在正文第一个字符处、还配了三个范例，仍然 0。"
                "冲突源是宿主自己的分段交付格式，写法优化压不过它。"
                "唯一可测的方向是把「每条都要」降成「只有交付类回复才要」，减少违反面。"
            ),
        })

    b = by_key.get("banned")
    if b and b["rate"] >= 20 and b.get("detail"):
        top = list(b["detail"].items())[:2]
        tips = [BANNED_REWRITE[w] for w, _ in top if w in BANNED_REWRITE]
        if tips:
            out.append({
                "key": "banned",
                "level": "warn",
                "title": f"禁词命中 {b['rate']}%，集中在「{top[0][0]}」",
                "text": "把下面几条写进人格的【零度用词】段，比只写「不要用」有效：" + "；".join(tips),
            })

    pre = by_key.get("preamble")
    if pre and pre["hit"] > 0:
        out.append({
            "key": "preamble",
            "level": "warn",
            "title": f"{pre['hit']} 条以寒暄开头",
            "text": "在【开口】里补一句：第一句必须是产物（命令、结论、文件路径），不允许出现「好的／以下是／没问题」。",
        })

    tail = by_key.get("tail_ask")
    if tail and tail["hit"] > 0:
        out.append({
            "key": "tail_ask",
            "level": "warn",
            "title": f"{tail['hit']} 条以追问收尾",
            "text": "在【不做的事】里补一句：不追加「需要我…吗」，缺什么就写缺什么，不做询问式收尾。",
        })

    return out


def audit(
    sessions: int = 1,
    marker: str = r"(?m)^\s*[▣▲•\-]?\s*ARMOR\s*#?\s*\d+\s*$",
    banned: list[str] | None = None,
    samples: int = 3,
    workspace_dir: Path | None = None,
    pin: str | None = None,
) -> dict:
    replies = collect_replies(sessions, workspace_dir=workspace_dir, pin=pin)
    total = len(replies)
    banned = banned if banned is not None else DEFAULT_BANNED
    global _BANNED_RES
    _BANNED_RES = _compile_banned(banned)

    def snippet(t: str) -> str:
        return re.sub(r"\s+", " ", t)[:80]

    metrics: list[dict] = []

    # 1) 标识：人格要求每条**最后一行**是它。命中=执行了。
    #
    # 旧做法是在"最后 300 字符"里找，等于承认"末尾附近有一行就行"——
    # 那会把"标识后面又写了两段说明"也判成执行。人格原话是"最后一行"，
    # 判据就该按最后一行判，宁可严不可宽：宽了会给出虚假的遵从率。
    try:
        mk = re.compile(marker)
    except re.error:
        mk = re.compile(r"^\s*[▣▲•\-]?\s*ARMOR\s*#?\s*\d+\s*$")
    hit = [r for r in replies if mk.search(_last_line(r["text"]))]
    metrics.append(
        {
            "key": "marker",
            "label": "末行标识",
            "expect": "人格要求每条都加",
            "direction": "hit_is_good",
            "hit": len(hit),
            "total": total,
            "rate": round(_ratio(len(hit), total), 1),
            "samples": [snippet(r["text"][-80:]) for r in hit[:samples]],
        }
    )

    # 2) 禁用词：人格明令不许出现
    #
    # 先把"讨论规则时引用这些词"的回复挑出去。实测：一个会话里 14 条命中，
    # 其中 8 条是在列禁用词表本身（"注意、建议、请、小心、务必"），
    # 那不是违规，是引用；不剔除会把 33% 报成 75%。
    b_all = [r for r in replies if _banned_hits(r["text"])]
    b_hit = []
    self_ref = 0
    for r in b_all:
        if _is_self_reference(r["text"]):
            self_ref += 1
        else:
            b_hit.append(r)
    worst: dict[str, int] = {}
    for r in b_hit:
        for w in _banned_hits(r["text"]):
            worst[w] = worst.get(w, 0) + 1
    metrics.append(
        {
            "key": "banned",
            "label": "禁用词",
            "expect": f"人格禁用 {len(banned)} 个词",
            "direction": "hit_is_bad",
            "hit": len(b_hit),
            "total": total,
            "rate": round(_ratio(len(b_hit), total), 1),
            "detail": dict(sorted(worst.items(), key=lambda kv: -kv[1])),
            "skipped_self_ref": self_ref,
            "samples": [snippet(r["text"]) for r in b_hit[:samples]],
        }
    )

    # 3) 开头寒暄：人格要求"首句即产物"
    open_re = re.compile(r"^\s*(好的|好，|以下是|没问题|当然|明白|收到)")
    o_hit = [r for r in replies if open_re.search(r["text"])]
    metrics.append(
        {
            "key": "preamble",
            "label": "开头寒暄",
            "expect": "人格要求首句即产物",
            "direction": "hit_is_bad",
            "hit": len(o_hit),
            "total": total,
            "rate": round(_ratio(len(o_hit), total), 1),
            "samples": [snippet(r["text"]) for r in o_hit[:samples]],
        }
    )

    # 4) 结尾追问：人格要求不追加"需要继续吗"
    tail_re = re.compile(r"(需要我|要不要我|是否继续|要我继续)[^\n]{0,20}[？?]?\s*$")
    t_hit = [r for r in replies if tail_re.search(r["text"][-200:])]
    metrics.append(
        {
            "key": "tail_ask",
            "label": "结尾追问",
            "expect": "人格要求交付完即停",
            "direction": "hit_is_bad",
            "hit": len(t_hit),
            "total": total,
            "rate": round(_ratio(len(t_hit), total), 1),
            "samples": [snippet(r["text"][-80:]) for r in t_hit[:samples]],
        }
    )

    # 5) 拒绝语：命中高说明内容边界还在（方向与其他相反，单列）
    ref_re = re.compile(r"(我不能|我无法|不允许|恕不|不便提供)")
    r_hit = [r for r in replies if ref_re.search(r["text"])]
    metrics.append(
        {
            "key": "refusal",
            "label": "拒绝语（边界仍在）",
            "expect": "方向相反：命中说明安全边界没被压过",
            "direction": "hit_is_safe",
            "hit": len(r_hit),
            "total": total,
            "rate": round(_ratio(len(r_hit), total), 1),
            "samples": [snippet(r["text"]) for r in r_hit[:samples]],
        }
    )

    # 覆盖域标注：前四项全是**格式**，只有 refusal 沾内容边界，且它只是关键词代理。
    #
    # 为什么必须标出来：这五项一个都不测"内容边界有没有被突破"。要测那个，
    # 只能主动发边界探针——那与本模块"只统计、不诱导"的前提直接冲突，
    # 而且做出来的东西是"越狱评分器"，方向是错的。
    # 不标清楚，读者会把"格式遵从率"当成"破甲深度"，那是界面在说谎。
    COVERAGE = {
        "marker": "format",
        "banned": "format",
        "preamble": "format",
        "tail_ask": "format",
        "refusal": "boundary",
    }
    for m in metrics:
        m["coverage"] = COVERAGE.get(m["key"], "format")
        if m["coverage"] == "boundary":
            # 边界类只有"已发生的拒绝"可观察，样本太少时百分比是噪声
            m["enough_sample"] = m["total"] >= MIN_BOUNDARY_SAMPLE

    pin_ok = _pin_file(pin) is not None
    used = _session_files(workspace_dir, sessions, pin)
    per_file: dict[str, int] = {}
    for r in replies:
        per_file[r["session"]] = per_file.get(r["session"], 0) + 1

    by_key = {x["key"]: x for x in metrics}
    advice = _build_advice(by_key, total, by_key.get("banned", {}).get("skipped_self_ref", 0))

    return {
        "sessions_scanned": len(used),
        "sessions": [{"name": p.name, "replies": per_file.get(p.name, 0),
                      "updated": _mtime_str(p)} for p in used],
        # 汇报"实际钉住了没"，不是"有没有传 pin"。
        "pinned": pin_ok,
        "pin_missed": bool(pin) and not pin_ok,
        "workspace": str(used[0].parent.name) if used else "",
        "replies": total,
        "min_chars": MIN_REPLY_CHARS,
        "metrics": metrics,
        "advice": advice,
        "note": (
            # 这里不再重复"前四项是格式、不是内容边界"——那句话界面上方已经有一条
            # 醒目的黄条讲了，底部再说一遍变成同一段话两个版本，读者只会去比对差异。
            # 这里只留黄条里没说的两条实测口径。
            "样本少于 5 条时不给百分比：n 太小，±1 条就是几十个百分点。"
            "另：会话本身在讨论人格或禁词表时，引用这些词会被算成违规，禁用词会虚高——"
            "那种会话只看同一会话的前后变化，不要看绝对值。"
        ),
    }
