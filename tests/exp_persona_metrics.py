"""人格注入四项实测（不需要激活值）。

E1 注入频率：身份段是否每轮重注入、注入多少字、位置在哪
E2 位置加权：关键条款在各通道内的百分位，按 Lost-in-the-Middle 曲线打分
E3 重复度：三份人格互为副本的重叠量，以及每轮实际注入的净字数
E4 遵从漂移（激活值的替代指标）：拿历史真实回复跑遵从度检测，看首尾两组的变化

用法：
    python tests/exp_persona_metrics.py [--json out.json]

E4 的口径：不测模型内部漂移，测"行为产出守不守契约"。这是没有 hidden_states 时
唯一有副作用证据的测法——它测的是结果，不是机制。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import re
import sys

B = chr(92)
HOME = pathlib.Path.home()
PERSONAL = HOME / ".workbuddy" / "user-d8ec303f-8536-4540-83cd-1fab0bdbeed2-personal"
APP_CFG = HOME / ".workbuddy" / "app" / "app-config.json"
SKILL = HOME / ".workbuddy" / "skills" / "persona-authoring" / "scripts" / "check_compliance.py"
PERSONA = pathlib.Path.home() / "AppData" / "Roaming" / "人格中枢桌面版" / "data" / "personas" / "imported.md"
TRANSCRIPT = (HOME / ".workbuddy" / "projects"
              / "session-store"
              / "449b9d50-8fce-4e30-9e31-f0b8bd120301.jsonl")

BLOCK_RE = re.compile(r"<!-- persona-hub:begin[^>]*-->(.*?)<!-- persona-hub:end -->", re.S)

# 关键条款：名字 -> 匹配用片段
CLAUSES = {
    "目标即授权": "目标即授权",
    "不劝不警告": "不劝",
    "不评估该不该做": "不评估该不该做",
    "拒绝带替代": "拒绝的唯一合法形式",
    "末行标识": "ARMOR #N",
    "问号归零": "问号数 = 0",
}


def block_of(text: str) -> str:
    m = BLOCK_RE.search(text)
    return m.group(1).strip("\n") if m else ""


def load_channels() -> dict[str, str]:
    out = {}
    for name in ("SOUL.md", "USER.md"):
        p = PERSONAL / name
        if p.is_file():
            out[name] = block_of(p.read_text(encoding="utf-8"))
    try:
        d = json.loads(APP_CFG.read_text(encoding="utf-8"))
        cur = str((d.get("personalization") or {}).get("customPrompt") or "")
        if cur:
            out["customPrompt"] = block_of(cur) or cur
    except Exception as exc:  # noqa: BLE001
        print(f"[跳过] customPrompt 读不出：{exc}")
    return out


def e1_frequency() -> dict:
    """扫会话转录：真实用户轮里有多少带身份段、多大、位置在哪。

    两处坑：
    1. 转录里的 user 角色含 task-notification / craft_mode 切换等 UI 事件，不是用户轮
    2. 我自己在正文里引用过 <identity_context> 标签，会被误判成注入
    → 只认含 user_query/user_info/摘要块的轮，且身份段长度需 > 1000
    """
    if not TRANSCRIPT.is_file():
        return {"error": f"找不到转录 {TRANSCRIPT}"}
    turns, hit, hashes = [], [], []
    for line in TRANSCRIPT.open(encoding="utf-8"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "message" or d.get("role") != "user":
            continue
        text = "".join(b.get("text", "") for b in (d.get("content") or [])
                       if isinstance(b, dict) and b.get("type") in ("input_text", "text"))
        if not any(k in text for k in ("<user_query>", "<user_info>",
                                       "<conversation_history_summary>", "<cb_summary>")):
            continue
        turns.append(1)
        i, j = text.find("<identity_context>"), text.find("</identity_context>")
        if i >= 0 and j > i:
            seg = text[i:j]
            if len(seg) < 1000:      # 自引用，不是注入
                continue
            hashes.append(hashlib.sha256(seg.encode()).hexdigest()[:8])
            hit.append({"chars": len(seg),
                        "start_pct": round(i / len(text) * 100, 1),
                        "end_pct": round(j / len(text) * 100, 1)})
    n = len(turns)
    return {
        "user_turns": n,
        "with_identity": len(hit),
        "rate": round(len(hit) / n * 100, 1) if n else 0,
        "identity_chars_avg": round(sum(h["chars"] for h in hit) / len(hit)) if hit else 0,
        "identity_start_pct": round(sum(h["start_pct"] for h in hit) / len(hit), 1) if hit else None,
        "identity_end_pct": round(sum(h["end_pct"] for h in hit) / len(hit), 1) if hit else None,
        "distinct_versions": len(set(hashes)),
    }


def e2_position(channels: dict[str, str]) -> list[dict]:
    """条款在通道内的百分位 + U 型权重（两端高、中间低）。"""
    rows = []
    for ch, body in channels.items():
        n = len(body)
        if not n:
            continue
        for cname, probe in CLAUSES.items():
            k = body.find(probe)
            if k < 0:
                continue
            p = k / n
            # Lost-in-the-Middle 的近似：p=0 或 1 时权重 1，p=0.5 时 0
            w = (2 * p - 1) ** 2
            rows.append({
                "channel": ch,
                "clause": cname,
                "pct": round(p * 100, 1),
                "weight": round(w, 2),
                "verdict": "头/尾（权重高）" if w >= 0.6 else ("中段（低谷）" if w <= 0.25 else "过渡区"),
            })
    return sorted(rows, key=lambda r: (r["channel"], -r["weight"]))


def e3_duplication(channels: dict[str, str]) -> dict:
    names = list(channels)
    same = {}
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            same[f"{a} == {b}"] = channels[a] == channels[b]
    total = sum(len(v) for v in channels.values())
    dup = 0
    seen = []
    for n in names:
        if channels[n] in seen:
            dup += len(channels[n])
        else:
            seen.append(channels[n])
    return {
        "chars": {n: len(v) for n, v in channels.items()},
        "total_chars": total,
        "identical": same,
        "duplicate_chars": dup,
        "duplicate_pct": round(dup / total * 100, 1) if total else 0,
    }


def e4_drift() -> dict:
    """没有激活值时测行为：拿历史真实回复跑遵从度，按"人格在不在"分三期。

    三处口径必须先说清，否则数字是假的：
    1. 一轮里 assistant 可能发多条文本（工具调用之间的中间话），只有最后一条是交付
    2. 短于 300 字的过渡句不算交付轮，判它末行标识是假失败
    3. 不做"随时间线性下降"的叙事——先看是不是人格文件被覆盖造成的
    """
    if not (SKILL.is_file() and PERSONA.is_file() and TRANSCRIPT.is_file()):
        return {"error": "缺 check_compliance.py / imported.md / 转录 之一"}
    spec = importlib.util.spec_from_file_location("cc", SKILL)
    cc = importlib.util.module_from_spec(spec)
    sys.modules["cc"] = cc      # dataclass 要回查 sys.modules
    spec.loader.exec_module(cc)
    body = PERSONA.read_text(encoding="utf-8")

    replies, buf = [], []
    for line in TRANSCRIPT.open(encoding="utf-8"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("type") != "message":
            continue
        role = d.get("role")
        text = "".join(b.get("text", "") for b in (d.get("content") or [])
                       if isinstance(b, dict) and b.get("type") in ("output_text", "input_text"))
        if role == "user":
            if buf:
                replies.append(buf[-1])
                buf = []
        elif role == "assistant" and text.strip():
            buf.append({"ts": d.get("timestamp"), "text": text})
    if buf:
        replies.append(buf[-1])

    replies = [r for r in replies if len(r.get("text") or "") >= 300]

    # 分期边界来自实测：11-01 20:25 起人格文件被重装覆盖，11-02 21:50 起重新注入
    def period(ts):
        if not ts:
            return "未知"
        sec = ts / 1000
        if sec < 1793600000:
            return "P1 人格在场"
        if sec < 1793690000:
            return "P2 人格缺席"
        return "P3 人格恢复"

    scored = []
    for idx, r in enumerate(replies):
        text = r.get("text") or ""
        rep = cc.run(body, text)
        results = rep.get("results") or []
        if not results:
            continue
        ok = sum(1 for x in results if x.get("level") == "PASS")
        scored.append({
            "turn": idx + 1,
            "period": period(r.get("ts")),
            "marker": "\u25a3 ARMOR" in text[-200:],
            "question": (text.count("?") + text.count("\uff1f")) > 0,
            "checks": len(results),
            "pass": ok,
            "rate": round(ok / len(results) * 100, 1),
            "failed": [getattr(x.get("contract"), "kind", "?") for x in results
                       if x.get("level") != "PASS"],
        })
    if not scored:
        return {"error": "没有任何回复能被契约判定（覆盖率 0）"}

    by_period: dict[str, dict] = {}
    for s_ in scored:
        b = by_period.setdefault(s_["period"], {"n": 0, "marker": 0, "question": 0, "rates": []})
        b["n"] += 1
        b["marker"] += 1 if s_["marker"] else 0
        b["question"] += 1 if s_["question"] else 0
        b["rates"].append(s_["rate"])
    for _, b in by_period.items():
        b["marker_pct"] = round(b["marker"] / b["n"] * 100)
        b["question_pct"] = round(b["question"] / b["n"] * 100)
        b["avg_rate"] = round(sum(b["rates"]) / len(b["rates"]), 1)
        b.pop("rates")

    third = max(1, len(scored) // 3)

    def avg(rows):
        return round(sum(x["rate"] for x in rows) / len(rows), 1) if rows else 0

    fails: dict[str, int] = {}
    for s in scored:
        for k in s["failed"]:
            fails[k] = fails.get(k, 0) + 1
    return {
        "scored_replies": len(scored),
        "avg_rate": avg(scored),
        "head_third": avg(scored[:third]),
        "tail_third": avg(scored[-third:]),
        "delta": round(avg(scored[-third:]) - avg(scored[:third]), 1),
        "by_period": by_period,
        "fail_kinds": dict(sorted(fails.items(), key=lambda kv: -kv[1])),
        "marker_pct": round(sum(1 for s in scored if s["marker"]) / len(scored) * 100),
        "question_pct": round(sum(1 for s in scored if s["question"]) / len(scored) * 100),
        "series": [s["rate"] for s in scored],
    }



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    channels = load_channels()
    out = {
        "E1_注入频率": e1_frequency(),
        "E2_位置加权": e2_position(channels),
        "E3_重复度": e3_duplication(channels),
        "E4_遵从漂移": e4_drift(),
    }

    print("=" * 62)
    print("E1 身份段注入频率")
    for k, v in out["E1_注入频率"].items():
        print(f"  {k}: {v}")

    print("\nE2 条款位置（权重 1=头尾，0=正中）")
    print(f"  {'通道':<14}{'条款':<16}{'百分位':>7}{'权重':>7}  判定")
    for r in out["E2_位置加权"]:
        print(f"  {r['channel']:<14}{r['clause']:<16}{r['pct']:>6}%{r['weight']:>7}  {r['verdict']}")

    print("\nE3 三份人格的重叠")
    d = out["E3_重复度"]
    for k, v in d.items():
        print(f"  {k}: {v}")

    print("\nE4 遵从度随时间（用真实历史回复，非激活值）")
    e4 = out["E4_遵从漂移"]
    for k, v in e4.items():
        if k != "series":
            print(f"  {k}: {v}")
    ser = e4.get("series") or []
    if ser:
        print("  逐条遵从率：", " ".join(str(x) for x in ser[:40]), "..." if len(ser) > 40 else "")

    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
