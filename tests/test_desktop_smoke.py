#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""桌面版功能巡检：对着安装包里那份内核，把全部接口逐个跑一遍。

为什么不用源码 hub.py 起服务：交付形态是桌面版，用户跑的是包里的 exe。
两者隔着 PyInstaller 一层（--windowed、_MEIPASS、sys.stdout 为 None），
源码跑通不代表包里跑通 —— 这一层已经踩过（pythonw 下 print 抛 AttributeError）。

用法：
    python tests/test_desktop_smoke.py                     # 自动找桌面版内置内核
    python tests/test_desktop_smoke.py --exe dist/persona-hub.exe
    python tests/test_desktop_smoke.py --keep              # 跑完不删沙盒，便于看现场

两条边界，脚本自己不做：
    * /api/tpl/apply|revert 会改进 WorkBuddy 安装目录里的模板（真实宿主文件）
    * /api/cloudmem 的写接口会改 ~/.workbuddy/memory 下的记忆文件（真实用户文件）
  这两类只能在进程内单测里用 monkeypatch 换根（test_tpl.py / test_selfcheck_cloud.py），
  此处只做只读查询，并在报告里标明"写路径未在本脚本覆盖"。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS = 0
FAIL = 0
FAILS: list[str] = []
SKIPPED: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name}  ->  {str(detail)[:220]}")
        print(f"  \u2717 {name}  ->  {str(detail)[:220]}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


INSTALLED_EXE = Path(r"./")


def find_exe(explicit: str | None) -> Path:
    """找被测内核。默认用**项目里**的构建产物。

    为什么不直接用安装目录那份：用户明示不要动 `./`，
    那是他自己装的、随时会自行更新。这里只把它当"同源比对对象"——
    读 sha256 确认是同一份二进制，不执行、不写。
    """
    if explicit:
        p = Path(explicit)
        if not p.is_absolute():
            p = ROOT / p
        return p
    for c in (ROOT / "dist" / "persona-hub.exe", ROOT / "persona-hub.exe"):
        if c.is_file():
            return c
    raise SystemExit("找不到内核 exe，用 --exe 指定")


def same_as_installed(exe: Path) -> str:
    """报告被测 exe 与安装目录那份是否同一份（只读，不执行）。"""
    import hashlib

    def h(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]

    try:
        if not INSTALLED_EXE.is_file():
            return "安装目录里没有内核 exe（可能未装或装在别处）"
        a, b = h(exe), h(INSTALLED_EXE)
        if a == b:
            return f"与安装目录那份逐字节相同（sha256 {a}…）"
        return f"与安装目录那份不同：被测 {a}… / 已装 {b}…（以项目构建为准，用户自会更新）"
    except OSError as exc:
        return f"比对失败：{exc}"


class Client:
    def __init__(self, port: int) -> None:
        self.base = f"http://127.0.0.1:{port}"
        # 代理：本机被注入了 HTTP_PROXY，测本机服务必须绕开，否则请求会被转走
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def raw(self, method: str, path: str, body: bytes | None = None,
            ctype: str = "application/json", headers: dict | None = None,
            timeout: int = 60) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, data=body, method=method)
        if body is not None:
            req.add_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc).encode()

    def api(self, method: str, path: str, payload: dict | None = None,
            **kw) -> tuple[int, dict]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        st, raw = self.raw(method, path, body, **kw)
        try:
            return st, json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return st, {"_raw": raw[:200].decode("utf-8", "replace")}

    def ok(self, method: str, path: str, payload: dict | None = None, **kw) -> dict:
        st, d = self.api(method, path, payload, **kw)
        if st != 200 or not d.get("ok"):
            raise AssertionError(f"{method} {path} -> {st} {json.dumps(d, ensure_ascii=False)[:200]}")
        return d.get("data") or {}


def wait_ready(c: Client, limit: float = 45.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < limit:
        st, d = c.api("GET", "/api/ping")
        if st == 200 and (d.get("data") or {}).get("app"):
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------- 各组

def group_state(c: Client) -> None:
    print("\n[1] 探活与状态")
    st, d = c.api("GET", "/api/ping")
    data = d.get("data") or {}
    check("ping 返回 app/version", st == 200 and bool(data.get("app")) and bool(data.get("version")),
          f"{st} {data}")
    check("ping 带 page_alive 字段（外壳判断窗口在不在靠它）", "page_alive" in data, list(data))
    st, d = c.api("GET", "/api/ping?page=1")
    check("打 page=1 后 page_alive 变真", (d.get("data") or {}).get("page_alive") is True,
          (d.get("data") or {}).get("page_alive"))

    s = c.ok("GET", "/api/state")
    for k in ("version", "app", "personas", "adapters", "vault", "data_root", "detected_count"):
        check(f"state 含 {k}", k in s, list(s))
    check("state.personas 是列表", isinstance(s.get("personas"), list), type(s.get("personas")))
    check("adapters 每项有 id 与 targets",
          all("id" in a and "targets" in a for a in s.get("adapters") or []),
          (s.get("adapters") or [{}])[0].keys() if s.get("adapters") else "空")

    doc = c.ok("GET", "/api/doctor")
    check("doctor 有结论字段", bool(doc), list(doc)[:6])
    bud = c.ok("GET", "/api/budget")
    check("budget 返回结构体", isinstance(bud, dict), type(bud))
    adp = c.ok("GET", "/api/adapters")
    check("adapters 列表非空且带 targets", isinstance(adp, list) and adp and "targets" in adp[0],
          f"{len(adp) if isinstance(adp, list) else adp}")
    logs = c.ok("GET", "/api/logs")
    check("logs 有 lines 字段", isinstance(logs, dict) and "lines" in logs, list(logs)[:6])


def group_persona(c: Client, home: Path) -> str:
    print("\n[2] 人格库：新建 / 读 / 改 / 取名 / 删")
    pid = c.ok("POST", "/api/personas", {"name": "巡检人格", "body": "【巡检】正文，含变量 {{name}}。"})["id"]
    check("新建返回 id", bool(pid), pid)

    lst = c.ok("GET", "/api/personas")
    check("列表里能看到新建的", any(x["id"] == pid for x in lst), [x["id"] for x in lst][:5])

    d = c.ok("GET", f"/api/personas/{pid}")
    check("详情含 name/body", d.get("name") == "巡检人格" and "巡检" in str(d.get("body")), d.keys())

    c.ok("PUT", f"/api/personas/{pid}", {"name": "巡检人格2", "body": "【巡检】改过。"})
    d2 = c.ok("GET", f"/api/personas/{pid}")
    check("改名与正文都生效", d2.get("name") == "巡检人格2" and "改过" in str(d2.get("body")),
          (d2.get("name"), str(d2.get("body"))[:20]))

    b = c.ok("GET", f"/api/personas/{pid}/body")
    check("渲染正文接口可用", isinstance(b, dict) and "body" in b, list(b))

    st, d3 = c.api("POST", "/api/personas", {"name": "", "body": ""})
    check("空人格被拒（400 EMPTY_PERSONA）", st == 400, f"{st} {d3}")

    # 导入：从沙盒里造一个 md
    src = home / "imported-by-test.md"
    src.write_text("---\nid: x\n---\n\n【导入】正文\n", encoding="utf-8", newline="\n")
    imp = c.ok("POST", "/api/personas/import", {"path": str(src), "name": "导入来的"})
    check("导入返回新 id", bool(imp.get("id")), imp)
    st, d4 = c.api("POST", "/api/personas/import", {"path": str(home / "not-exist.md")})
    check("导入不存在的文件返回 404", st == 404, f"{st} {d4}")

    c.ok("DELETE", f"/api/personas/{imp['id']}")
    lst2 = c.ok("GET", "/api/personas")
    check("删除后列表里没有它", all(x["id"] != imp["id"] for x in lst2), imp["id"])

    snaps = c.ok("GET", "/api/snapshots")
    items = snaps.get("items") if isinstance(snaps, dict) else snaps
    check("删人格前自动留了快照", bool(items), f"{type(snaps)} {str(snaps)[:120]}")
    return pid


def group_inject(c: Client, home: Path, pid: str) -> str:
    print("\n[3] 注入：自定义目标 / 预览 / 写入 / 检测 / 开关 / 移除")
    target = home / "inject-target.md"
    target.write_text("# 原本内容\n\n第一段。\n", encoding="utf-8", newline="\n")

    aid = c.ok("POST", "/api/custom", {"name": "巡检靶", "paths": [str(target)]})["id"]
    check("自定义目标建立成功", bool(aid), aid)

    st, d = c.api("POST", "/api/custom", {"paths": [str(home)]})
    check("目录不能当目标（400）", st == 400, f"{st} {d}")

    pv = c.ok("POST", "/api/preview", {"persona_id": pid, "adapters": [aid], "keep_others": True})
    for k in ("files", "total_added", "total_removed"):
        check(f"preview 含 {k}", k in pv, list(pv))
    check("preview 不写盘（目标文件未变）",
          target.read_text(encoding="utf-8") == "# 原本内容\n\n第一段。\n", "文件已被改动")

    ap = c.ok("POST", "/api/apply", {"persona_id": pid, "adapters": [aid], "keep_others": True})
    check("apply 返回 written/steps", "written" in ap and "steps" in ap, list(ap))
    text = target.read_text(encoding="utf-8")
    check("目标文件出现区块标记", "persona-hub:begin" in text and "persona-hub:end" in text,
          text[:80])
    check("原有内容保留", "# 原本内容" in text, text[:60])

    inj = c.ok("GET", "/api/injections")
    items = inj.get("items") or []
    mine = [x for x in items if x.get("adapter_id") == aid]
    check("注入检测能扫到自己写的这条", len(mine) == 1, f"{len(mine)} / 共 {len(items)}")
    if mine:
        fid = mine[0]["id"]
        # 字段名以**前端实际读的**为准（web/app.js 用 state==='off'、toggleable、deletable、preview），
        # 不是"看着应该叫什么"。接口和界面各说各话时，界面会静默显示错的状态。
        for k in ("id", "adapter_id", "kind", "state", "toggleable", "deletable", "preview"):
            check(f"注入项含 {k}", k in mine[0], list(mine[0]))
        check("未停用时 state 不是 off", mine[0].get("state") != "off", mine[0].get("state"))

        c.ok("POST", "/api/injections/toggle", {"id": fid, "enable": False})
        off = c.ok("GET", "/api/injections")
        me2 = [x for x in (off.get("items") or []) if x.get("adapter_id") == aid]
        check("关掉后 state 变成 off", bool(me2) and me2[0].get("state") == "off",
              me2[0].get("state") if me2 else "没找到")
        c.ok("POST", "/api/injections/toggle", {"id": fid, "enable": True})
        on = c.ok("GET", "/api/injections")
        me3 = [x for x in (on.get("items") or []) if x.get("adapter_id") == aid]
        check("再开回来 state 不再是 off", bool(me3) and me3[0].get("state") != "off",
              me3[0].get("state") if me3 else "没找到")

        rm = c.ok("POST", "/api/injections/remove", {"ids": [fid]})
        check("移除返回 removed 列表", "removed" in rm, list(rm))
        check("移除后文件里没有区块",
              "persona-hub:begin" not in target.read_text(encoding="utf-8"),
              target.read_text(encoding="utf-8")[:80])

    # 再写一次，留给备份组用
    c.ok("POST", "/api/apply", {"persona_id": pid, "adapters": [aid], "keep_others": True})
    return aid


def group_backup(c: Client, aid: str) -> None:
    print("\n[4] 备份与还原")
    snaps = c.ok("GET", "/api/snapshots")
    items = snaps.get("items") if isinstance(snaps, dict) else snaps
    check("快照列表非空", bool(items), f"{len(items) if isinstance(items, list) else items}")
    if not items:
        return
    sid = items[0].get("id") or items[0].get("snapshot_id")
    check("快照项带 id 与时间", bool(sid) and "time" in items[0], items[0].keys())
    v = c.ok("GET", f"/api/snapshots/{sid}/verify")
    check("校验快照返回结论", isinstance(v, dict) and v, list(v)[:6])

    r = c.ok("POST", "/api/restore", {"snapshot_id": sid})
    check("还原返回 items", "items" in r and isinstance(r["items"], list), list(r))
    st, d = c.api("POST", "/api/restore", {"snapshot_id": "../../../etc"})
    check("非法快照 id 被拒（不 200）", st != 200, f"{st} {str(d)[:120]}")

    g = c.ok("POST", "/api/vault/gc")
    check("gc 返回 removed_snapshots/freed_bytes",
          "removed_snapshots" in g and "freed_bytes" in g, list(g))


def group_diag(c: Client) -> None:
    print("\n[5] 诊断四块（只读部分）")
    vf = c.ok("GET", "/api/verify")
    check("生效自检返回 verdict", bool(vf.get("verdict")), list(vf))
    # 这七个键必须与 web/app.js 的 VERDICT_STYLE 完全一致：
    # 多一个键界面显示成"无法判定"，少一个键前端取不到样式。
    check("verdict 是界面认识的七种之一",
          vf.get("verdict") in {"ok", "carried", "stale", "partial", "missing",
                                "no_injection", "no_session"},
          vf.get("verdict"))

    au = c.ok("GET", "/api/audit?sessions=1")
    for k in ("replies", "sessions", "pinned"):
        check(f"破甲度含 {k}", k in au, list(au))
    check("sessions 每项带 updated", all("updated" in x for x in au.get("sessions") or []),
          (au.get("sessions") or [{}])[0])

    st, d = c.api("GET", "/api/audit?session=*")
    check("通配符会话名被拒（400）", st == 400, f"{st} {d}")

    tpl = c.ok("GET", "/api/tpl")
    for k in ("host", "templates", "expert", "record", "stale"):
        check(f"模板状态含 {k}", k in tpl, list(tpl))

    cm = c.ok("GET", "/api/cloudmem")
    check("云记忆状态含 exists/backups", "exists" in cm and "backups" in cm, list(cm))
    st, d = c.api("POST", "/api/cloudmem/restore", {"path": r"C:\Windows\win.ini"})
    check("越界还原被拒（返回 ok:false 或 4xx）", not (st == 200 and (d.get("data") or {}).get("ok")),
          f"{st} {str(d)[:120]}")

    SKIPPED.append("/api/tpl/apply|revert、/api/cloudmem 写接口（会改真实宿主文件，由进程内单测覆盖）")


def group_update(c: Client) -> None:
    print("\n[6] 更新通道")
    st, d = c.api("GET", "/api/update/check", timeout=30)
    data = d.get("data") or {}
    check("update/check 有明确结论（不 500）", st in (200, 400, 502), f"{st} {str(d)[:160]}")
    if st == 200:
        check("含 current 与 has_update", "current" in data and "has_update" in data, list(data))
        SKIPPED.append("update/download（会真的下几十 MB 并落盘，单独手工验）")
    else:
        SKIPPED.append(f"update/check 网络不可达（{st}），只确认了错误被兜住")


def group_edge(c: Client, home: Path, pid: str, aid_out: str) -> None:
    print("\n[7] 边界与畸形输入")
    st, d = c.api("GET", "/api/does-not-exist")
    check("未知路由 404", st == 404, f"{st} {d}")

    st, _ = c.raw("POST", "/api/cloudmem", b'{"block":"x"}', ctype="text/plain")
    check("非 JSON Content-Type 被拒（415，防 CSRF）", st == 415, st)

    st, _ = c.raw("POST", "/api/cloudmem", b"{not json", ctype="application/json")
    check("坏 JSON 得到 400 而不是 500", st == 400, st)

    st, _ = c.raw("GET", "/api/ping", headers={"Host": "evil.example.com"})
    check("伪造 Host 被拒（403，防 DNS rebinding）", st == 403, st)

    st, _ = c.raw("GET", "/../core/server.py")
    code = st
    leaked = False
    if st == 200:
        _, raw = c.raw("GET", "/../core/server.py")
        leaked = b"import " in raw or b"def main" in raw
    check("静态资源越界不吐源码", (not leaked) and code in (403, 404), f"{code} leaked={leaked}")

    # 大请求体走**正常路由**：600KB 中文（约 1.8MB 字节）要能存进去。
    # 这条当初写错过：用 PUT /api/personas（这个路由只有 GET/POST），
    # 结果测的其实是"路由不存在"，而不是"大正文能不能存"。
    big = "长" * 200000
    st, d = c.api("PUT", f"/api/personas/{pid}", {"name": "大正文", "body": big})
    check("600KB 正文能存（不该误判为超限）", st == 200, f"{st} {str(d)[:120]}")

    # 大 body 打到"不存在的路由"和"方法不对的路由"：必须拿到 404 JSON，
    # 而不是 ConnectionAborted / Failed to fetch。
    # 这是一处真缺陷：服务端没读请求体就回错误并关连接，客户端还在写就被 RST，
    # 用户看到"网络错误"，真实原因只是一个 404。修法见 core/server.py::_drain_body。
    for method, path, label in (("PUT", "/api/nope", "不存在的路由"),
                                ("PUT", "/api/apply", "方法不对（apply 只收 POST）")):
        body = json.dumps({"name": "x", "body": "长" * 200000}, ensure_ascii=False).encode()
        st, raw = c.raw(method, path, body)
        ok = st == 404 and b"NOT_FOUND" in raw
        check(f"大 body 打到{label}也能拿到 404 JSON", ok, f"{st} {raw[:100]!r}")

    # 注意：adapters 为空时先报 MISSING_TARGET（400），测不到人格校验。必须带一个真目标。
    st, d = c.api("POST", "/api/preview", {"persona_id": "不存在的", "adapters": [aid_out]})
    check("不存在的人格给 404/422 而不是 500", st in (404, 422), f"{st} {d}")


def group_static(c: Client) -> None:
    print("\n[8] 静态资源")
    st, raw = c.raw("GET", "/")
    check("首页可取", st == 200 and b"<!DOCTYPE html" in raw[:300], f"{st} {len(raw)}B")
    for f in ("/app.js", "/style.css"):
        st, raw = c.raw("GET", f)
        check(f"{f} 可取且非空", st == 200 and len(raw) > 1000, f"{st} {len(raw)}B")
    st, raw = c.raw("GET", "/app.js")
    check("app.js 是桌面版要的版本（含 page=1 心跳）", b"api/ping?page=1" in raw, "未找到心跳标记")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=None)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    exe = find_exe(args.exe)
    print("=" * 60)
    print("桌面版功能巡检")
    print("=" * 60)
    print(f"被测内核: {exe}  ({exe.stat().st_size:,} B)")
    print(f"同源比对: {same_as_installed(exe)}")

    work = Path(tempfile.mkdtemp(prefix="ph-smoke-"))
    home = work / "home"
    home.mkdir()
    port = free_port()
    env = dict(os.environ)
    env["PERSONA_HUB_HOME"] = str(home)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)          # 本机被注入的代理会把 127.0.0.1 请求转走
    proc = subprocess.Popen([str(exe), "--no-gui", "--port", str(port), "--quiet"],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    c = Client(port)
    try:
        if not wait_ready(c):
            out = b""
            try:
                out = proc.stdout.read(2000) if proc.stdout else b""
            except Exception:  # noqa: BLE001
                pass
            print("服务没起来。进程输出：", out[:800].decode("utf-8", "replace"))
            return 2
        print(f"已就绪，端口 {port}\n")

        group_state(c)
        pid = group_persona(c, home)
        aid = group_inject(c, home, pid)
        group_backup(c, aid)
        group_diag(c)
        group_update(c)
        group_edge(c, home, pid, aid)
        group_static(c)

        # bye 放在最后：它会关掉服务
        st, d = c.api("POST", "/api/bye")
        check("bye 返回 bye:true", st == 200 and (d.get("data") or {}).get("bye") is True, f"{st} {d}")
        time.sleep(2)
        check("bye 之后进程自己退出", proc.poll() is not None, f"poll={proc.poll()}")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                proc.kill()
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"\n沙盒保留在 {work}")

    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if SKIPPED:
        print("\n本脚本不覆盖（已注明由哪条链负责）：")
        for x in SKIPPED:
            print("  ·", x)
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
