"""全链路验证 —— 一个脚本跑完六组检查，并守护已注入的人格不被改动。

为什么单独写这个脚本（而不是直接跑 tests/）：
1. tests/ 里全是单元/集成测试，覆盖不到"真实数据只读检查"和"写路径演练"这两段
2. 用户明确要求"开发完多验证几次"，需要一个能反复跑、结果可比的入口
3. **最关键**：跑任何验证之前先记录真实 USER.md 的哈希，跑完再比。
   只要变了就立刻报警并从备份还原——这是本项目唯一不能出错的资产。

用法：
    python tools/verify-all.py            # 全部六组
    python tools/verify-all.py --pass 3   # 只跑第 3 组

分组：
    1  完整性守护（文件未被改动）
    2  静态检查（语法、版本号一致、图标存在）
    3  真实数据只读（自检/破甲度/云记忆/预算/人格正文）
    4  沙盒写路径演练（并存不覆盖旧人格 → 替换只留一份 → 从快照还原）
    5  全套单元与集成测试
    6  判据有效性（变异测试：把修复改回去，必须报红）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = Path.home()
_IDENTITY_DIR = HOME / ".workbuddy" / "user-d8ec303f-8536-4540-83cd-1fab0bdbeed2-personal"
USER_MD = _IDENTITY_DIR / "USER.md"
# 2026-10-31 起人格不再放 USER.md，改放 SOUL.md（专家会话要读它，详见 core/tplpatch.py）。
# 守护必须跟着一起搬 —— 只守 USER.md 等于守了一个空文件。
SOUL_MD = _IDENTITY_DIR / "SOUL.md"

LIVE_MD: Path | None = None   # group1 认出来的人格文件

PASS = FAIL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name}  {detail}")
        print(f"  ✗ {name}  -> {detail}")
    return cond


def sha(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Api:
    """不带代理的 HTTP 客户端——本机环境有 HTTP_PROXY 注入，必须显式绕过。"""

    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _req(self, path: str, method: str = "GET", payload: dict | None = None) -> dict:
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        with self.opener.open(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            raise RuntimeError(f"{path}: {body.get('message')}")
        return body["data"]


def start_server(home: Path | None) -> tuple[subprocess.Popen, int, Api]:
    port = free_port()
    env = dict(os.environ)
    if home is not None:
        env["PERSONA_HUB_HOME"] = str(home)
    proc = subprocess.Popen(
        [sys.executable, "-u", "hub.py", "--no-gui", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    api = Api(port)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            api._req("/api/ping")
            return proc, port, api
        except Exception:  # noqa: BLE001
            if proc.poll() is not None:
                out = (proc.stdout.read() or b"").decode("utf-8", "replace")
                raise RuntimeError(f"服务启动即退出：{out[:400]}")
            time.sleep(0.25)
    proc.kill()
    raise RuntimeError("服务在 40 秒内没有就绪")


def stop_server(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except Exception:  # noqa: BLE001
        proc.kill()


# ---------------------------------------------------------------- 第 1 组
def group1() -> str:
    """完整性守护：先认人格住哪个文件，再核它在不在。

    2026-10-31 人格从 USER.md 搬到 SOUL.md（专家会话要读 SOUL，见 core/tplpatch.py）。
    所以这里不能写死 USER.md —— 写死的话，搬家当天这条就会对着一份空白画像说
    "人格块没了"，而真正的人格就在旁边的 SOUL.md 里。
    """
    print("\n[1] 完整性守护")
    global LIVE_MD
    LIVE_MD = None
    for cand in (SOUL_MD, USER_MD):
        if not cand.is_file():
            continue
        t = cand.read_text(encoding="utf-8", errors="replace")
        if "persona-hub:begin" in t:
            LIVE_MD = cand
            break

    if LIVE_MD is None:
        check("找到人格所在文件（SOUL.md 或 USER.md）", False,
              "两处都没有 persona-hub 区块 —— 人格丢了，先查备份")
        return ""

    text = LIVE_MD.read_text(encoding="utf-8", errors="replace")
    ids = [ln.split("id=")[1].split()[0] for ln in text.splitlines() if "persona-hub:begin" in ln]
    before = sha(LIVE_MD)
    check(f"人格所在文件可读（{LIVE_MD.name}）", True)
    check("人格块还在（不是空文件）", bool(ids), f"找到 {len(ids)} 个区块")
    check("只有一个人格块（并存遗留会互相打架）", len(ids) <= 1, f"实际 {ids}")
    print(f"    人格: {ids or '(无)'}   住在 {LIVE_MD.name}   sha256={before[:16]}")
    return before


# ---------------------------------------------------------------- 第 2 组
def group2() -> None:
    print("\n[2] 静态检查")
    import ast

    for rel in ("core/server.py", "core/verify.py", "core/audit.py", "core/cloudmem.py", "hub.py"):
        p = ROOT / rel
        try:
            ast.parse(p.read_text(encoding="utf-8"))
            check(f"{rel} 语法", True)
        except SyntaxError as exc:
            check(f"{rel} 语法", False, str(exc))

    # 换行风格守卫：源码一律 LF。
    # 为什么单列一条：脚本用 read_text/write_text 改文件时，Windows 默认把整个文件
    # 转成 CRLF。后果不是"看着别扭"，而是**按多行字符串定位的变异锚点全部失效**——
    # 上一轮就这样让 M4/M8 静默跳过，而变异测试恰好是唯一能证明判据没摆设的东西。
    crlf = [str(f.relative_to(ROOT)) for f in ROOT.rglob("*")
            if f.is_file() and f.suffix.lower() in {".py", ".js", ".html", ".css", ".md", ".tpl"}
            and not any(x in f.parts for x in (".build", "build", "dist", "site", "data", "node_modules",
                                    "release", "win-unpacked"))
            and b"\r\n" in f.read_bytes()]
    check("源码换行统一为 LF", not crlf, f"CRLF 文件：{crlf[:5]}")

    # 版本号真源与产物自报一致
    src = (ROOT / "core" / "paths.py").read_text(encoding="utf-8")
    ver = ""
    for line in src.splitlines():
        if line.startswith("VERSION ="):
            ver = line.split("=")[1].strip().strip('"')
    check("版本号真源可读", bool(ver), src[:60])

    # 对外三处必须同一版：桌面 package.json、下载页文案、线上更新清单。
    # 这条以前没人管，实测漂过——页面上写着 v0.3.7，链的却是 0.3.11 的包，
    # 用户下完看到"版本号对不上"，第一反应是新版没发出去。
    pkg = ROOT / "desktop" / "package.json"
    desk = ""
    if pkg.is_file():
        desk = (json.loads(pkg.read_text(encoding="utf-8")).get("version") or "").strip()
    check("桌面版本号可读", bool(desk), desk)

    idx = ROOT / "site" / "index.html"
    if desk and idx.is_file():
        html = idx.read_text(encoding="utf-8", errors="replace")
        # 只钉三个"会被用户当成当前版本"的位置。
        # 历史沿革段（"为什么 0.3.6 才加向导"）本来就该提旧版本，不能一概而论。
        pinned = {
            "顶栏版本": re.search(r'<span class="ver">桌面版 v?([\d.]+)', html),
            "下载卡版本": re.search(r'<span class="dl-ver">v?([\d.]+)</span>', html),
            "下载卡文件名": re.search(r"人格中枢桌面版-Setup-([\d.]+)\.exe", html),
        }
        bad = [f"{k}={m.group(1)}" for k, m in pinned.items() if not m or m.group(1) != desk]
        check("页面版本号与真源一致", not bad, f"不一致：{bad}，真源 {desk}")
        # 文件名带 8 位内容哈希：托管站的部署是**纯增量**，从不删旧文件。
        # 实测站上 0.3.5 到 0.3.13 九个安装包全都还在、都能下。
        # 名字里带哈希之后，新版不再落进"按版本号可枚举"的序列。
        check("页面链接带内容哈希",
              bool(re.search(rf"persona-desktop-{re.escape(desk)}-[0-9a-f]{{8}}-setup\.exe", html)),
              "页面没有 persona-desktop-<版本>-<sha8>-setup.exe 形式的链接")

    feed = ROOT / "site" / "desktop-version.json"
    if desk and feed.is_file():
        try:
            j = json.loads(feed.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            j = {}
            check("更新清单可解析", False, str(exc))
        if j:
            check("更新清单版本号与真源一致", j.get("latest") == desk,
                  f"清单 {j.get('latest')} vs 真源 {desk}")
            m = re.search(rf"persona-desktop-{re.escape(desk)}-([0-9a-f]{{8}})-setup\.exe",
                          str(j.get("url")))
            check("更新清单指向本版安装包且带哈希", bool(m), str(j.get("url")))
            if m:
                check("文件名里的哈希与清单 sha256 前 8 位一致",
                      m.group(1) == str(j.get("sha256") or "")[:8],
                      f"文件名 {m.group(1)} vs sha256 {str(j.get('sha256'))[:8]}")
            size = j.get("size") or 0
            local = ROOT / "site" / f"persona-desktop-{desk}-{m.group(1)}-setup.exe" if m else None
            if local and local.is_file():
                check("清单大小与本地包一致", local.stat().st_size == size,
                      f"清单 {size} vs 本地 {local.stat().st_size}")
            else:
                check("本地能找到清单指向的安装包", False,
                      f"缺 persona-desktop-{desk}-*-setup.exe")
    # 更新源同源守卫。site/ 是「改完再上架」的源目录，它一旦落后于云通道，
    # 下次上架就会把清单里的下载地址指回旧站：客户端的 source 在新站、url 在旧站，
    # 而 ticket 只在同 host 传递（见 core/update.py:_with_ticket），
    # 结果是「检查更新能过、点下载 403」这种只在真机才暴露的静默失效。
    # 所以把本仓库的四个常量钉死成同一个 host：内核源、外壳源、两份清单的 url。
    _src_py = ROOT / "core" / "update.py"
    _upd_js = ROOT / "desktop" / "src" / "updater.js"
    _hosts = {}
    if _src_py.is_file():
        _m = re.search(r'DEFAULT_SOURCE\s*=\s*\(\s*\n?\s*"([^"]+)"',
                       _src_py.read_text(encoding="utf-8"))
        if _m:
            _hosts["内核 DEFAULT_SOURCE"] = urllib.parse.urlparse(_m.group(1)).netloc
        else:
            check("内核更新源可解析", False, "core/update.py 里没找到 DEFAULT_SOURCE")
    if _upd_js.is_file():
        _m = re.search(r"FEED_URL\s*=\s*'([^']+)'", _upd_js.read_text(encoding="utf-8"))
        if _m:
            _hosts["外壳 FEED_URL"] = urllib.parse.urlparse(_m.group(1)).netloc
        else:
            check("外壳更新源可解析", False, "desktop/src/updater.js 里没找到 FEED_URL")
    for _f in (ROOT / "site" / "version.json", ROOT / "site" / "desktop-version.json"):
        if _f.is_file():
            try:
                _u = str(json.loads(_f.read_text(encoding="utf-8")).get("url") or "")
            except json.JSONDecodeError:
                _u = ""
            _hosts[f"清单 {_f.name}"] = urllib.parse.urlparse(_u).netloc
    # 页面上的下载按钮同理：相对路径会从页面所在的旧站取包，
    # 旧站一旦下线按钮就全死，而且双份 110 MB 白占空间。
    _idx = ROOT / "site" / "index.html"
    if _idx.is_file():
        _m = re.search(r'class="dl-btn"\s+href="([^"]+)"', _idx.read_text(encoding="utf-8", errors="replace"))
        if _m:
            _hosts["页面下载按钮"] = urllib.parse.urlparse(_m.group(1)).netloc

    _stale = sorted({h for h in _hosts.values() if "workbuddy.host" in h})
    check("清单与更新源指向同一云通道", not _stale,
          f"旧站域名残留：{_stale}（各值 {_hosts}）" if _stale else str(sorted(set(_hosts.values()))))
    _uniq = {h for h in _hosts.values() if h}
    check("更新源 host 三处一致", len(_uniq) == 1,
          f"不一致：{_hosts}" if len(_uniq) != 1 else next(iter(_uniq)))

    # site/ 里的每个文件都会被原样公开。放进去一个 .py 就等于把服务端脚本挂到网上——
    # 实测 serve_range.py 曾是 HTTP 200 可下载，而它根本没在跑（线上响应头是
    # CloudStudio Gateway，且不支持 Range），纯属死文件加暴露。
    site_dir = ROOT / "site"
    if site_dir.is_dir():
        allowed = {".html", ".json", ".exe", ".zip", ".png", ".svg", ".ico", ".txt", ".webmanifest"}
        stray = sorted(
            p.name for p in site_dir.iterdir()
            if p.is_file() and p.suffix.lower() not in allowed
        )
        check("站点目录没有会泄露源码的文件", not stray, f"不该公开：{stray}")

    exe = ROOT / "persona-hub.exe"
    if exe.is_file():
        # 产物可能比源码旧，只提示不判失败——真正的判据是"在线包与本地一致"
        print(f"    源码版本 {ver}，exe 修改时间 {time.strftime('%H:%M', time.localtime(exe.stat().st_mtime))}")

    icon = ROOT / "web" / "favicon.ico"
    check("图标存在（外壳要用）", icon.is_file(), str(icon))

    # 界面接线：绑了事件的元素存不存在（本机做不了 CDP，这是能静态判的那一半）
    ui = subprocess.run([sys.executable, "tools/check_ui_wiring.py"], cwd=str(ROOT),
                        capture_output=True, text=True, timeout=120)
    tail = next((ln.strip() for ln in reversed((ui.stdout or "").splitlines())
                 if "界面接线检查" in ln), "")
    check(f"界面接线 {tail or '(无输出)'}", ui.returncode == 0, (ui.stdout or "")[-300:])
    ui_mut = subprocess.run([sys.executable, "tools/check_ui_wiring.py", "--selftest"],
                            cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    check("界面接线判据能抓到错", ui_mut.returncode == 0, (ui_mut.stdout or "")[-200:])


# ---------------------------------------------------------------- 第 3 组
def group3() -> None:
    print("\n[3] 真实数据只读（不写任何文件）")
    proc, _, api = start_server(None)
    try:
        st = api._req("/api/state")
        check("state 可用", isinstance(st.get("personas"), list), str(st.keys())[:80])
        check("能读到人格列表", len(st["personas"]) > 0, f"{len(st['personas'])} 个")

        v = api._req("/api/verify")
        check("自检返回判定", v.get("verdict") in
              {"ok", "carried", "stale", "partial", "missing", "no_injection", "no_session"},
              str(v.get("verdict")))
        check("自检带出上下文人格", isinstance(v.get("in_context"), list), str(v.get("in_context"))[:80])
        print(f"    自检: {v.get('verdict')}  磁盘 {[x['id'] for x in v.get('on_disk', [])]}")

        a = api._req("/api/audit?sessions=1")
        check("破甲度返回五项指标", len(a.get("metrics") or []) == 5, str(len(a.get("metrics") or [])))
        check("破甲度不给总分（方向相反不能相加）", "score" not in a, str(list(a.keys())[:10]))
        check("破甲度标明会话来源", isinstance(a.get("sessions"), list), str(a.get("sessions"))[:60])
        print(f"    样本 {a.get('replies')} 条：" +
              " ".join(f"{m['label']}={m['rate']}%" for m in a.get("metrics", [])))

        c = api._req("/api/cloudmem")
        check("云记忆可读", "exists" in c, str(list(c.keys())[:6]))
        if c.get("exists"):
            check("云记忆双写一致（Memory Block 与 RAW_JSON 同值）", c.get("raw_ok") is True, str(c.get("raw_ok")))

        # 预算返回 {"items": [...]}，不是裸列表——第一版按列表断言，误报了一次
        b = api._req("/api/budget")
        items = b.get("items") if isinstance(b, dict) else b
        check("预算接口可用", isinstance(items, list) and len(items) > 0, str(type(b)))
        if isinstance(items, list) and items:
            # 字段名照接口实际返回写（limit/total），不要照自己的想象写
            need = {"limit", "total", "remaining", "persona_chars"}
            check("预算项字段齐全", need <= set(items[0].keys()), str(sorted(items[0].keys())))

        pid = st["personas"][0]["id"]
        body = api._req(f"/api/personas/{pid}/body")
        check("人格正文可取（云记忆填入要用）", body.get("chars", 0) > 0, str(body.get("chars")))
    finally:
        stop_server(proc)


# ---------------------------------------------------------------- 第 4 组
def group4() -> None:
    """写路径演练：这是最容易毁掉用户数据的部分，必须真跑一遍。

    模拟用户当前状态：目标文件里已经有一段注入好的人格（id=keepme）。
    然后验证三件事：
        并存   → 旧人格必须活下来（上一轮修过的那个"新旧覆盖"就是这里）
        替换   → 只剩当前这一份
        快照   → 每次写入都留了可还原的快照
    """
    print("\n[4] 沙盒写路径演练（不碰真实文件）")
    work = Path(tempfile.mkdtemp(prefix="ph-verify-write-"))
    home = work / "home"
    home.mkdir()
    target = work / "target.md"
    target.write_text(
        "# 某个客户端的配置\n\n原有用户内容，不能被吃掉。\n\n"
        "<!-- persona-hub:begin id=keepme hash=aaaa1111 -->\n【旧人格】这一段必须活下来\n"
        "<!-- persona-hub:end -->\n",
        encoding="utf-8",
    )

    proc, _, api = start_server(home)
    try:
        aid = api._req("/api/custom", "POST", {"name": "沙盒目标", "paths": [str(target)]})["id"]
        pa = api._req("/api/personas", "POST", {"name": "验证甲", "body": "【甲】" + "甲内容。" * 20})["id"]
        pb = api._req("/api/personas", "POST", {"name": "验证乙", "body": "【乙】" + "乙内容。" * 20})["id"]

        def blocks() -> list[str]:
            t = target.read_text(encoding="utf-8")
            return [ln.split("id=")[1].split()[0]
                    for ln in t.splitlines() if "persona-hub:begin" in ln]

        r = api._req("/api/apply", "POST", {"persona_id": pa, "adapters": [aid], "keep_others": True})
        check("写入甲成功", r.get("writable", r.get("ok")) is not None and bool(r.get("written")), str(r)[:120])
        got = blocks()
        check("并存：旧人格 keepme 仍在", "keepme" in got, str(got))
        check("并存：甲也在", pa in got, str(got))
        check("原有用户内容没被吃掉", "不能被吃掉" in target.read_text(encoding="utf-8"))

        api._req("/api/apply", "POST", {"persona_id": pb, "adapters": [aid], "keep_others": True})
        got = blocks()
        check("第二个人格并存后共三份", len(got) == 3, str(got))

        api._req("/api/apply", "POST", {"persona_id": pb, "adapters": [aid], "keep_others": False})
        got = blocks()
        check("替换：只剩当前这一份", got == [pb], str(got))
        check("替换：旧人格确实被移除", "keepme" not in got, str(got))

        # 路由是 /api/snapshots（我原先按 /api/vault 猜，404——又一个"凭想象写接口"的例子）
        v = api._req("/api/snapshots")
        snaps = v if isinstance(v, list) else v.get("items", v.get("snapshots", []))
        check("写操作留了可还原快照", len(snaps) >= 3, f"{len(snaps)} 份")

        # 快照能还原：这是"人格被弄没"的最后一道保险
        if snaps:
            sid = snaps[-1].get("id") or snaps[-1].get("name")
            try:
                api._req("/api/restore", "POST", {"snapshot_id": sid})
                check("能从快照还原", True)
            except Exception as exc:  # noqa: BLE001
                check("能从快照还原", False, str(exc)[:120])

        inj = api._req("/api/injections")
        rows = inj if isinstance(inj, list) else inj.get("items", [])
        check("注入管理能列出人格块", isinstance(rows, list) and len(rows) >= 1, str(type(inj)))
    finally:
        stop_server(proc)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- 第 5 组
def group5() -> None:
    print("\n[5] 全套单元与集成测试")
    suites = [
        ("selftest", []),
        ("test_position", []),
        ("test_channel", []),
        ("test_edge", []),
        ("test_update", []),
        ("test_selfcheck_cloud", []),
        ("test_audit", []),
        ("test_hardening", []),
        ("test_import_encoding", []),
        ("test_tpl", []),
        ("test_heal", []),
        ("test_instance", []),
        # DSH 注入自检判据：造多帧 zstd 会话，验 ok/stale/missing/no_injection 四态，
        # 外加「只解第一帧」的变异必须被判死。不碰真实 ~/.dsh。
        ("test_verify_dsh", []),
        # 桌面版功能巡检：对着 dist/persona-hub.exe（用户实际跑的那份二进制）跑全部接口。
        # 为什么不能只靠源码里的那些：两者隔着 PyInstaller 一层，
        # windowed 下 sys.stdout 是 None 这类差异，只在打包后才暴露。
        ("test_desktop_smoke", []),
        ("test_e2e", ["--port"]),
    ]
    for name, extra in suites:
        if extra:
            port = free_port()
            home = Path(tempfile.mkdtemp(prefix=f"ph-{name}-"))
            proc = subprocess.Popen(
                [sys.executable, "-u", "hub.py", "--no-gui", "--port", str(port)],
                cwd=str(ROOT),
                env={**os.environ, "PERSONA_HUB_HOME": str(home)},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(4)
            try:
                out = subprocess.run(
                    [sys.executable, f"tests/{name}.py", "--port", str(port)],
                    cwd=str(ROOT), capture_output=True, text=True, timeout=600,
                )
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except Exception:  # noqa: BLE001
                    proc.kill()
                shutil.rmtree(home, ignore_errors=True)
        else:
            out = subprocess.run(
                [sys.executable, f"tests/{name}.py"],
                cwd=str(ROOT), capture_output=True, text=True, timeout=600,
            )
        text = (out.stdout or "") + (out.stderr or "")
        line = ""
        for ln in text.splitlines():
            if "通过" in ln and "项" in ln:
                line = ln.strip()
        ok = out.returncode == 0 and "失败 0 项" in line
        check(f"{name:22s} {line or '(无汇总行)'}", ok, line or text[-200:])
        if not ok:
            print("      输出尾部:", text[-400:].replace("\n", " | "))

    # 前端"统计对象跟着对话走"的行为用例：node + 最小 DOM 桩跑真实 web/app.js。
    # 为什么不并进 test_audit：那条链是 Python 里的统计口径，这条是界面状态机，
    # 两边都可能单独坏；而这个 bug 正是从界面看出来的。
    # 前端 / 外壳的 node 用例：都跑真实代码（app.js、updater.js），不重写一份逻辑。
    node = shutil.which("node")
    for js_name in ("test_audit_ui.js", "test_updater.js", "test_bounds.js", "test_verify_ui.js"):
        js = ROOT / "tests" / js_name
        stem = js_name[:-3]
        if node and js.is_file():
            out = subprocess.run([node, str(js)], cwd=str(ROOT), capture_output=True,
                                 text=True, timeout=300)
            text = (out.stdout or "") + (out.stderr or "")
            line = next((ln.strip() for ln in reversed(text.splitlines()) if "通过" in ln), "")
            check(f"{stem:22s} {line or '(无汇总行)'}", out.returncode == 0,
                  line or text[-200:])
        else:
            check(f"{stem:22s} 跳过（未找到 node 或文件）", False, f"node={node} js={js.is_file()}")


# ---------------------------------------------------------------- 第 6 组
def group6() -> None:
    """判据有效性：把修复逐一改回去，看测试会不会报红。

    为什么单列一组而不是并进第 5 组：第 5 组回答"现在对不对"，
    本组回答"改坏了会不会被发现"。后者会**临时改写源码**（跑完恢复），
    语义不同，失败时的处置也不同——它红了说明判据是摆设，不是产品坏了。
    """
    print("\n[6] 判据有效性（变异测试，会临时改源码再恢复）")
    cases = [
        ("test_mutation_limit", "读取窗口判据（None / 4000 两种变异）"),
        ("test_hardening_mutation", "加固判据（H1–H13 + M13–M18 逐一回退）"),
        ("test_update_mutation", "更新链路判据（断线 / 写失败 / 引号 / 安装器）"),
        ("test_bounds_mutation", "窗口位置判据（屏幕缺失 / 尺寸超界）"),
        ("test_import_mutation", "导入判据（GBK / 空文件 / 解码说明）"),
    ]
    for name, desc in cases:
        out = subprocess.run([sys.executable, f"tests/{name}.py"], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=1800)
        text = (out.stdout or "") + (out.stderr or "")
        line = ""
        for ln in text.splitlines():
            if "抓到错" in ln or ("通过" in ln and "项" in ln):
                line = ln.strip()
        check(f"{name:22s} {desc}", out.returncode == 0, line or text[-200:])
        if line:
            print(f"    {line}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pass", dest="only", type=int, default=0, help="只跑第 N 组")
    args = ap.parse_args()

    # 两份身份文件都在**开跑前**取快照。写成"收尾时取、收尾时比"会永远相等，
    # 看着有一条守护，实际什么也没守。
    snapshots = {p: sha(p) for p in (USER_MD, SOUL_MD) if p.is_file()}
    before = group1()
    if not before:
        print("\n[中止] 找不 USER.md，先恢复备份再继续")
        return 2

    if args.only in (0, 2):
        group2()
    if args.only in (0, 3):
        group3()
    if args.only in (0, 4):
        group4()
    if args.only in (0, 5):
        group5()
    if args.only in (0, 6):
        group6()

    print("\n[收尾] 再核一次身份文件有没有被动过")
    all_ok = True
    for path, mark in snapshots.items():
        now = sha(path)
        all_ok &= check(f"{path.name} 与验证前逐字节一致", mark == now,
                        f"{mark[:16]} -> {now[:16]}")
    if all_ok:
        print("    人格完好，未被本轮验证改动")
    else:
        print("    !! 身份文件发生变化，请立刻从 persona-guard 备份还原")

    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
