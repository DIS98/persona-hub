"""端到端测试：通过真实 HTTP 接口跑完整链路。

存在理由：单元测试测的是模块，而用户踩的坑几乎都在「接口串起来」的地方——
比如"新建中文名人格会互相覆盖"、"注入第二个人格把第一个清掉"。
这两条都在下面各有一组用例。

为了不碰用户的真实客户端配置，注入目标用**自定义目标**指向沙盒里的文件，
顺便把自定义目标的全链路（新增 → 注入 → 还原 → 删除）一起验了。

用法：
    python hub.py --no-gui --port 8799     # 另起一个终端
    python tests/test_e2e.py [--port 8799]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PASS = FAIL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILS.append(name + (f"  ({detail})" if detail else ""))
        print(f"  \u2717 {name}" + (f"  -> {detail}" if detail else ""))
    return cond


# 本机进程级 HTTP_PROXY 会把 127.0.0.1 也代理走（实测被 Clash 拦成 502），
# 所以这里显式清空代理处理器——否则测试在开着代理的机器上永远拿不到 JSON。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Api:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with _OPENER.open(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                raise RuntimeError(f"{method} {path} 返回非 JSON（HTTP {exc.code}）: {raw[:200]}") from None
        if not payload.get("ok"):
            err = payload.get("error") or {}
            raise RuntimeError(f"{err.get('code')}: {err.get('message')}")
        return payload["data"]

    def get(self, path: str) -> dict:
        return self.call("GET", path)

    def post(self, path: str, body: dict) -> dict:
        return self.call("POST", path, body)

    def delete(self, path: str) -> dict:
        return self.call("DELETE", path)


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check_api_contract(api: "Api") -> None:
    """契约级回归：结果必须被带回来，空输入必须被拒。

    这两条都是 2026-11-01 实测出的真 bug，而当时 493 项用例全绿 ——
    说明既有用例覆盖的是"设计好的路径对不对"，覆盖不到
    "返回值在不在"和"空 body 会怎样"。补在这里，因为这里连着真服务。
    """
    print("\n[0] 接口契约（返回值 / 空输入）")

    def raw(method: str, path: str, data: bytes | None = None):
        req = urllib.request.Request(api.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with _OPENER.open(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))

    st, d = raw("POST", "/api/cloudmem/restore", b'{"path":"C:/Windows/win.ini"}')
    check("还原接口把结果带回来（不是 data:null）", d.get("data") is not None, str(d)[:120])
    check("越界路径还原被拒（data.ok=False）",
          (d.get("data") or {}).get("ok") is False, str(d)[:160])

    st, d = raw("POST", "/api/personas", b"")
    check("空 body 新建人格 → 400", st == 400, f"{st} {str(d)[:120]}")
    check("错误码是 EMPTY_PERSONA",
          (d.get("error") or {}).get("code") == "EMPTY_PERSONA", str(d)[:160])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    args = ap.parse_args()
    api = Api(args.port)

    # 服务没起就直说，别让它崩在 json.loads 上——那个报错完全看不出真实原因。
    try:
        with _OPENER.open(api.base + "/api/ping", timeout=5) as resp:
            resp.read()
    except Exception as exc:
        print(f"[!] 连不上 {api.base}（{type(exc).__name__}）。")
        print(f"    先在另一个终端执行：python hub.py --no-gui --port {args.port}")
        return 2

    check_api_contract(api)

    sandbox = Path(os.environ.get("TEMP", "/tmp")) / "persona-hub-e2e"
    shutil.rmtree(sandbox, ignore_errors=True)
    sandbox.mkdir(parents=True, exist_ok=True)

    created_personas: list[str] = []
    created_customs: list[str] = []

    # 基线快照：清理时只能删「本次测试新出现的」条目。
    # 绝不能按 id 前缀去猜谁是自己建的——这么猜过一次，
    # 把用户自己的「墨」（id 恰好叫 persona）当成测试残留删了。
    base_personas = {p["id"] for p in api.get("/api/personas")}
    base_customs = {a["id"] for a in api.get("/api/state")["adapters"] if a.get("custom")}

    try:
        # ============================================================ 连通性
        print("\n[A] 服务连通性")
        ping = api.get("/api/ping")
        check("ping 返回时间戳", isinstance(ping.get("t"), (int, float)))
        state = api.get("/api/state")
        check("state 有版本号", bool(state.get("version")))
        check(
            "适配器带 custom 标记（前端据此决定能否删除）",
            all("custom" in a for a in state["adapters"]),
            str(state["adapters"][0].keys() if state["adapters"] else "空"),
        )

        # ============================================================ 自定义目标
        print("\n[B] 自定义目标客户端")
        t1 = sandbox / "rules.md"
        t1.write_text("# 我的规则\n\n原有内容，不能被吃掉。\n", encoding="utf-8", newline="\n")
        orig_hash = sha(t1)

        c1 = api.post("/api/custom", {"name": "沙盒目标甲", "paths": [str(t1)]})
        created_customs.append(c1["id"])
        check("新增自定义目标返回合法 id", bool(c1.get("id")), str(c1))
        check("返回路径数正确", c1.get("count") == 1, str(c1))

        # 中文名再建一个：id 必须不同（跟人格那个覆盖 bug 同根）
        t2 = sandbox / "rules2.md"
        c2 = api.post("/api/custom", {"name": "沙盒目标乙", "paths": [str(t2)]})
        created_customs.append(c2["id"])
        check("第二个中文名目标拿到不同 id", c1["id"] != c2["id"], f"{c1['id']} vs {c2['id']}")

        st = api.get("/api/state")
        ids = [a["id"] for a in st["adapters"]]
        check("两个自定义目标都出现在状态里", c1["id"] in ids and c2["id"] in ids)
        check(
            "自定义目标被标记 custom=True",
            all(a["custom"] for a in st["adapters"] if a["id"] in (c1["id"], c2["id"])),
        )
        check("自定义目标出现在检测结果里（用户明确指定就算可用）",
              next(a for a in st["adapters"] if a["id"] == c1["id"])["detected"])

        # ============================================================ 新建人格（覆盖 bug 的核心场景）
        print("\n[C] 新建两个人格（中文名，只写正文不写名字）")
        p1 = api.post("/api/personas", {"name": "甲人格", "body": "【甲】你要简洁。"})
        created_personas.append(p1["id"])
        p2 = api.post("/api/personas", {"name": "乙人格", "body": "【乙】你要严谨。"})
        created_personas.append(p2["id"])
        check("两个中文名人格 id 不同", p1["id"] != p2["id"], f"{p1['id']} vs {p2['id']}")
        check("第一个是新建而不是覆盖", p1.get("created") is True, str(p1))

        # 只写正文、名字留空：不该被拦，应自动给默认名
        p3 = api.post("/api/personas", {"name": "", "body": "只有正文没有名字。"})
        created_personas.append(p3["id"])
        lst = api.get("/api/personas")
        row = next((x for x in lst if x["id"] == p3["id"]), None)
        check("没有名字也能建成功", row is not None, str(p3))
        check("自动补了默认名", bool(row and row["name"]), row["name"] if row else "")
        check("正文写入正确", row and row["chars"] > 0, str(row))

        # ============================================================ 删除必须留备份
        print("\n[C2] 删除人格会先备份（数据丢失事故的回归测试）")
        tmp_p = api.post("/api/personas", {"name": "待删测试", "body": "这段内容删掉之后必须能拿回来。"})
        before_cnt = len(api.get("/api/snapshots")["items"])
        d = api.delete("/api/personas/" + urllib.parse.quote(tmp_p["id"]))
        check("删除返回了快照 id", bool(d.get("snapshot_id")), str(d))
        check("确实删掉了", d.get("deleted") is True, str(d))
        after_cnt = len(api.get("/api/snapshots")["items"])
        check("快照数量 +1", after_cnt == before_cnt + 1, f"{before_cnt} -> {after_cnt}")

        try:
            api.get("/api/personas/" + urllib.parse.quote(tmp_p["id"]))
            check("删掉后查不到该人格", False)
        except RuntimeError as e:
            check("删掉后查不到该人格", "NOT_FOUND" in str(e) or "不存在" in str(e), str(e))

        if d.get("snapshot_id"):
            api.post("/api/restore", {"snapshot_id": d["snapshot_id"]})
            back = api.get("/api/personas/" + urllib.parse.quote(tmp_p["id"]))
            check("还原后人格回来了", back.get("id") == tmp_p["id"], str(back.get("id")))
            check("正文逐字拿回", "这段内容删掉之后必须能拿回来" in back.get("body", ""),
                  back.get("body", "")[:60])
        created_personas.append(tmp_p["id"])

        # ============================================================ 多人格注入共存
        print("\n[D] 两个人格注入同一个文件（必须共存）")
        plan = api.post("/api/preview", {
            "persona_id": p1["id"],
            "adapters": [c1["id"]],
        })
        check("预览不落盘", sha(t1) == orig_hash)
        check("预览有目标文件", len(plan["files"]) == 1, str(plan["files"]))

        # 这一组验的是「显式共存」；默认语义是「一个通道只留一份」
        r1 = api.post("/api/apply", {"persona_id": p1["id"], "adapters": [c1["id"]], "keep_others": True})
        check("第一个人格写入成功", r1["ok"] and r1["written"] == 1, str(r1.get("message")))
        text1 = t1.read_text(encoding="utf-8")
        check("文件里出现甲的内容", "【甲】" in text1)
        check("原有内容没被吃掉", "原有内容，不能被吃掉" in text1)

        r2 = api.post("/api/apply", {"persona_id": p2["id"], "adapters": [c1["id"]], "keep_others": True})
        check("第二个人格写入成功", r2["ok"] and r2["written"] == 1, str(r2.get("message")))

        # 生效时机的判据（2026-10-30 栽过一次）：
        # 曾经写过"USER.md 每轮重读、不用重启"，依据只是一次观察，用户实测推翻了它。
        # 这两条把"不许再宣称不用重启"钉死，避免哪天有人又把它写回去。
        warns2 = r2.get("warnings") or []
        check("写入后提示要重开客户端", any("重开" in w for w in warns2), str(warns2))
        check("提示里不许再出现「不用重启」", not any("不用重启" in w for w in warns2), str(warns2))
        text2 = t1.read_text(encoding="utf-8")
        check("甲仍在文件里（没被覆盖）", "【甲】" in text2, text2[:200])
        check("乙也在文件里", "【乙】" in text2)
        check("两个区块标记各一处", text2.count("persona-hub:begin") == 2,
              f"实际 {text2.count('persona-hub:begin')} 处")
        check("原有内容仍在", "原有内容，不能被吃掉" in text2)

        # 重复注入第二个：不该新增区块
        api.post("/api/apply", {"persona_id": p2["id"], "adapters": [c1["id"]], "keep_others": True})
        text3 = t1.read_text(encoding="utf-8")
        check("重复注入不产生重复区块", text3.count("persona-hub:begin") == 2,
              f"实际 {text3.count('persona-hub:begin')} 处")

        # ============================================================ 注入管理能看见
        print("\n[E] 注入管理识别出这两个人格")
        inj = api.get("/api/injections")
        self_items = [f for f in inj["items"] if f["kind"] == "self"]
        # 只认写进本次沙盒目标的那些。
        # 不能按 persona_id 过滤：本工具给无名人格分配的第一个 id 就是 "persona"，
        # 用户自己真实注入的那份也常常叫 "persona"，两者会撞在一起——
        # 踩过一次，表现为「测试在本机跑红、在别人机器上跑绿」。
        mine = [f for f in self_items if str(f["path"]).startswith(str(sandbox))]
        check("扫描结果里有两个自身注入", len(mine) == 2,
              str([f["persona_id"] for f in self_items]))

        # 关掉其中一个：内容必须真的移出文件
        api.post("/api/injections/toggle", {"id": mine[0]["id"], "enable": False})
        text4 = t1.read_text(encoding="utf-8")
        off = text4.count("persona-hub:begin")
        check("停用后两个区块标记都还在（可再开回来）", off == 2, f"实际 {off} 处")
        check("被停用的那个带 state=off", "state=off" in text4, text4[:240])
        # 停用的正文必须真的不在文件里，否则"停用"只是自欺
        body_gone = ("【甲】" not in text4) != ("【乙】" not in text4)
        check("被停用那个的正文已从文件移出", body_gone, text4[:240])

        api.post("/api/injections/toggle", {"id": mine[0]["id"], "enable": True})
        back = t1.read_text(encoding="utf-8")
        check("重新启用后正文回来", "【甲】" in back and "【乙】" in back)

        # ============================================================ 还原
        print("\n[F] 还原到注入前")
        snaps = api.get("/api/snapshots")["items"]
        # 快照列表里的 files 是「文件数量」而非清单，所以按 label 定位：
        # apply:<p1> 的那条，记录的是这个文件**第一次被注入之前**的状态。
        cands = [s["id"] for s in snaps if s.get("label") == f"apply:{p1['id']}"]
        check("找到第一次注入的快照", bool(cands), str([s.get("label") for s in snaps[:5]]))
        target_snap = min(cands) if cands else None

        if target_snap:
            api.post("/api/restore", {"snapshot_id": target_snap})
            restored = t1.read_text(encoding="utf-8")
            check("还原后不再有本工具区块", "persona-hub:begin" not in restored, restored[:200])
            check("还原后原有内容回来", "原有内容，不能被吃掉" in restored, restored[:200])
            check(
                "还原到逐字节一致",
                sha(t1) == orig_hash,
                f"{sha(t1)[:12]} vs {orig_hash[:12]}",
            )

        # ============================================================ 清理
        print("\n[G] 删除自定义目标")
        gone = api.post("/api/custom/remove", {"id": c1["id"]})
        created_customs.remove(c1["id"])
        check("删除返回被删 id", gone.get("removed") == c1["id"], str(gone))
        st2 = api.get("/api/state")
        check("状态里不再有它", c1["id"] not in [a["id"] for a in st2["adapters"]])
        check("另一个自定义目标不受影响", c2["id"] in [a["id"] for a in st2["adapters"]])

        # 错误路径
        try:
            api.post("/api/custom/remove", {"id": "no-such-target"})
            check("删除不存在的目标应报错", False)
        except RuntimeError as e:
            check("删除不存在的目标返回 NOT_FOUND", "NOT_FOUND" in str(e), str(e))

        try:
            api.post("/api/custom", {"name": "空路径", "paths": []})
            check("空路径应被拒绝", False)
        except RuntimeError as e:
            check("空路径返回 BAD_REQUEST", "BAD_REQUEST" in str(e), str(e))

        # ============================================================ 预算
        print("\n[I] 读取窗口预算（写进去 ≠ 会被读到）")
        bud = api.get("/api/budget")
        check("budget 返回 items 列表", isinstance(bud.get("items"), list), str(type(bud.get("items"))))
        if bud["items"]:
            it = bud["items"][0]
            for k in ("limit", "total", "remaining", "persona_chars",
                      "user_chars", "readable_user", "user_starved", "personas"):
                check(f"字段 {k} 存在", k in it, str(list(it.keys())))
            check("remaining == limit - total",
                  it["remaining"] == it["limit"] - it["total"],
                  f"{it['remaining']} vs {it['limit']}-{it['total']}")
            check("人格占用 + 用户内容 == 全文",
                  it["persona_chars"] + it["user_chars"] == it["total"],
                  f"{it['persona_chars']}+{it['user_chars']} vs {it['total']}")
            check("可读用户内容不超过总长",
                  it["readable_user"] <= it["user_chars"], str(it["readable_user"]))
            check("可读用户内容不超过窗口",
                  it["readable_user"] <= it["limit"], str(it["readable_user"]))
            check("星标判据自洽（用户有内容但可读为 0 才算挤光）",
                  it["user_starved"] == (it["user_chars"] > 0 and it["readable_user"] == 0),
                  f"starved={it['user_starved']} user={it['user_chars']} readable={it['readable_user']}")
        else:
            print("  （本机没有声明读取窗口的目标，跳过明细检查）")

        # USER.md 的窗口是实测值，不是估的（2026-10-30：第 10,000 字符处精确截断）。
        # 这条判据钉住它，防止有人「顺手统一」把两个通道的上限合成一个数。
        user_items = [i for i in bud["items"] if i.get("file") == "USER.md"]
        check("USER.md 出现在预算里（说明它有窗口了）", bool(user_items),
              f"files={[i.get('file') for i in bud['items']]}")
        if user_items:
            check("USER.md 窗口 = 实测 10,000", user_items[0]["limit"] == 10000,
                  str(user_items[0]["limit"]))
        mem_items = [i for i in bud["items"] if i.get("file") == "MEMORY.md"]
        if mem_items:
            check("MEMORY.md 窗口仍是 4,000（两个通道不共用一个常量）",
                  mem_items[0]["limit"] == 4000, str(mem_items[0]["limit"]))

        # ============================================================ 单实例守卫
        # 用户实测症状：双击两次就有两个服务、三个窗口，两个实例都在改同一批配置。
        print("\n[J] 单实例守卫（已有实例时不许再起第二个服务）")
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import hub as hub_entry  # noqa: E402

        check("能识别出正在运行的实例", hub_entry._find_running_instance(args.port) == args.port,
              str(hub_entry._find_running_instance(args.port)))
        check("空闲端口不误判为实例",
              hub_entry._find_running_instance(args.port + 100, span=3) is None)
        # ping 必须自报身份，否则「端口被别的服务占着」会被误当成自己的实例
        ping = api.get("/api/ping")
        check("ping 自报 app 名", ping.get("app") == "人格中枢", str(ping.get("app")))
        check("ping 自报版本", bool(ping.get("version")), str(ping.get("version")))

        # Windows 的 SO_REUSEADDR 允许两个进程同时绑定同一端口，bind 不报错，
        # 所以「端口被占时自动往后找」这套逻辑必须靠主动探测，不能靠 bind 失败。
        # 实测踩到过：两个实例同时 LISTEN 8799，请求随机落到其中一个。
        from core.server import Server as _Srv  # noqa: E402

        check("端口探测能发现已被占用的端口", _Srv._port_busy(args.port) is True)
        check("空闲端口不被误判为占用", _Srv._port_busy(args.port + 100) is False)

        s2 = _Srv(args.port)
        p2 = s2.start()
        try:
            check("第二个实例不会挤到同一端口", p2 != args.port, f"落在 {p2}")
        finally:
            s2.stop()

        # 窗口外壳：优先 Chrome，Edge 兜底。
        # 这条判据的价值在"顺序"——如果哪天有人把 Edge 挪回前面，
        # 装了 Chrome 的机器会悄悄退回 Edge，用户看到的是"为什么又变成 Edge 了"。
        from core.paths import _chrome_candidates, browser_exe  # noqa: E402

        b = browser_exe()
        check("能定位到浏览器外壳", b is not None, str(b))
        if any(c.is_file() for c in _chrome_candidates()):
            check("装了 Chrome 就优先用 Chrome", b is not None and b[1] == "Chrome", str(b))
        else:
            check("没装 Chrome 时退回 Edge", b is not None, str(b))

        # ============================================================ 升级源
        print("\n[H] 在线升级源")
        upd = api.get("/api/update/check")
        check("默认源能读到版本信息", upd.get("latest") and not upd.get("error"),
              f"latest={upd.get('latest')} err={upd.get('error')}")
        check(
            "默认源不再 403",
            "403" not in str(upd.get("error", "")),
            str(upd.get("error")),
        )

        print("\n[K] 字段类型必须校验，不能静默转成字符串")

        def post_persona(payload: dict) -> tuple[int, dict]:
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                api.base + "/api/personas", data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with _OPENER.open(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read() or b"{}")

        code, res = post_persona({"name": "类型探针", "body": [1, 2, 3]})
        check("body 传数组被拒绝（不是存成字面量）", code == 400 and not res.get("ok"),
              f"{code} {str(res)[:100]}")
        code, res = post_persona({"name": "类型探针", "body": "x", "desc": {"a": 1}})
        check("desc 传对象被拒绝", code == 400 and not res.get("ok"),
              f"{code} {str(res)[:100]}")
        code, res = post_persona({"name": "类型探针", "body": "正常正文"})
        check("合法字符串仍然能建", code == 200 and res.get("ok"), f"{code} {str(res)[:80]}")
        if code == 200:
            created_personas.append(res["data"]["id"])

    finally:
        print("\n[Z] 清理测试数据")
        # 只删本次新建的（不在基线里的），别的一律不动
        for pid in created_personas:
            if pid in base_personas:
                continue
            try:
                api.delete("/api/personas/" + urllib.parse.quote(pid))
            except Exception:
                pass
        for cid in created_customs:
            if cid in base_customs:
                continue
            try:
                api.post("/api/custom/remove", {"id": cid})
            except Exception:
                pass
        left = [p["id"] for p in api.get("/api/personas") if p["id"] not in base_personas]
        check("测试人格已清理（未动任何原有条目）", not left, str(left))
        left_c = [a["id"] for a in api.get("/api/state")["adapters"]
                  if a.get("custom") and a["id"] not in base_customs]
        check("测试自定义目标已清理", not left_c, str(left_c))
        shutil.rmtree(sandbox, ignore_errors=True)
        print("  沙盒已删除")

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
