"""单实例与"关窗口就退出"的判据。

回答三个问题：
1. 后端能不能分清"有窗口在看"和"只是有 HTTP 请求"（后者包括外壳探活、测试脚本、curl）
2. 已经有实例、且窗口还开着时，再启动一次会不会再开一个窗口
3. 点 ✕ / 页面关闭时，后端会不会真的退出（而不是留一个后台进程）

前两条走真实 HTTP；第三条起子进程实测。桌面外壳那条只能做静态判据（本机起不了无头 Electron），
但至少能钉住"✕ 不许再变成隐藏"。
"""
from __future__ import annotations

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
sys.path.insert(0, str(ROOT))

import hub  # noqa: E402

PASS = 0
FAIL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name}  {detail}")
        print(f"  ✗ {name}  -> {detail}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(port: int, method: str, path: str, timeout: float = 5.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001
        return 0, {}


def start(port: int, home: Path) -> subprocess.Popen:
    env = {**os.environ, "PERSONA_HUB_HOME": str(home)}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "-u", "hub.py", "--no-gui", "--port", str(port)],
                            cwd=str(ROOT), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 40
    while time.time() < deadline:
        st, d = http(port, "GET", "/api/ping")
        if st == 200:
            return proc
        if proc.poll() is not None:
            raise RuntimeError("服务启动即退出")
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("服务没起来")


def group_a(port: int) -> None:
    print("\n[A] 区分「有窗口在看」和「只是有请求」")
    st, d = http(port, "GET", "/api/ping")
    data = d.get("data") or {}
    check("刚起来时没有窗口", data.get("page_alive") is False, str(data))
    check("page_idle 为 None（从没打过）", data.get("page_idle") is None, str(data.get("page_idle")))

    st, d = http(port, "GET", "/api/ping?page=1")
    data = d.get("data") or {}
    check("带 page=1 后认为窗口在线", data.get("page_alive") is True, str(data))
    idle0 = data.get("page_idle")
    check("page_idle 接近 0", idle0 is not None and idle0 < 2, str(idle0))

    # 外壳探活 / 测试脚本打的是不带参数的 ping，不能刷新"窗口在线"的计时
    time.sleep(2.2)
    st, d = http(port, "GET", "/api/ping")
    idle = (d.get("data") or {}).get("page_idle")
    check("不带 page 的 ping 不刷新窗口心跳", idle is not None and idle >= 2, str(idle))


def group_b(port: int, home: Path) -> None:
    print("\n[B] 已有实例时再启动，不许再开窗口 / 再起服务")
    calls: list[str] = []
    real_launch, real_shell_start = hub.launch_shell, None
    hub.launch_shell = lambda url: calls.append(url)      # 不真的开浏览器

    # 万一单实例判定失效，宁可在测试里炸掉，也不要去写真实宿主目录 / 抢端口
    class _Boom(Exception):
        pass

    def _no_server(*a, **k):
        raise _Boom("判定失效：走到了起服务的分支")

    real_server = hub.Server
    hub.Server = _no_server  # type: ignore[assignment]

    import core.heal as heal_mod
    real_heal = heal_mod.run_quiet
    heal_mod.run_quiet = lambda: heal_mod.EXIT_OK  # 绝不在测试里动宿主模板

    try:
        http(port, "GET", "/api/ping?page=1")            # 先让后端认为窗口在线
        rc = hub.main(["--port", str(port)])
        check("窗口在线时再启动：正常返回 0", rc == 0, str(rc))
        check("窗口在线时再启动：不再开窗口", not calls, str(calls))

        # 反例：窗口已经不在了（新建一个后端但没人打过 page=1），应当补开一个
        port2 = free_port()
        proc2 = start(port2, home / "second")
        try:
            calls.clear()
            rc2 = hub.main(["--port", str(port2)])
            check("没有窗口时：补开一个窗口", len(calls) == 1 and calls[0].startswith("http://127.0.0.1:"),
                  str(calls))
            check("没有窗口时：不开第二个服务（返回 0）", rc2 == 0, str(rc2))
            st, _ = http(port2, "GET", "/api/ping")
            check("补开窗口后原实例仍在", st == 200, str(st))
        finally:
            proc2.terminate()
            try:
                proc2.wait(timeout=8)
            except Exception:  # noqa: BLE001
                proc2.kill()
    finally:
        hub.launch_shell = real_launch
        hub.Server = real_server  # type: ignore[assignment]
        heal_mod.run_quiet = real_heal


def group_c(port: int, home: Path) -> None:
    print("\n[C] 关窗口 = 后端自己退出（不用另点退出）")
    # 这里测的是"判活"本身，不起浏览器、也不依赖 --no-gui（那条是明确的常驻服务意图）。
    # 做法是喂一个假 srv：一个用 /api/bye 的信号，一个用"太久没请求"的兜底。
    class _App:
        closing = False

    class _Srv:
        def __init__(self, closing_after: float | None, idle: float):
            self.app = _App()
            self.last_request = time.time() - idle
            self._closing_after = closing_after

    # 情形 1：页面发来 bye
    srv1 = _Srv(0.4, 0)
    import threading

    def flip():
        time.sleep(0.9)
        srv1.app.closing = True

    threading.Thread(target=flip, daemon=True).start()
    t0 = time.time()
    reason = hub._watch_until_closed(srv1, port, 3600)
    cost = time.time() - t0
    check("bye 一到就退出", reason.startswith("页面关闭"), reason)
    check("bye 之后 5 秒内退出", cost < 5, f"{cost:.1f}s")

    # 情形 2：bye 没发出去（浏览器被杀 / 崩溃），靠兜底
    srv2 = _Srv(None, idle=10_000)
    t0 = time.time()
    reason2 = hub._watch_until_closed(srv2, port, 5)
    check("没有 bye 时靠空闲兜底退出", "无页面活动" in reason2, reason2)
    check("兜底也只等一个阈值", time.time() - t0 < 4, f"{time.time() - t0:.1f}s")

    # 情形 3：真实 HTTP —— /api/bye 确实会置位（服务端口上的那个 App）
    st, d = http(port, "POST", "/api/bye")
    check("真实 /api/bye 应答", st == 200 and (d.get("data") or {}).get("bye") is True, str(d)[:100])


def group_e(home: Path) -> None:
    print("\n[E] 找不到 Chrome/Edge 时：回落默认浏览器，且关掉页签就退出")
    # 这一段是真实跑 main() 的：把 launch_shell 换成"没有浏览器"，看它会不会
    #   1) 正常回落到 webbrowser.open（原来的写法会在这里解包 None 直接抛 TypeError，
    #      打包成 --windowed 后表现成"双击没反应"）
    #   2) 页面发出 /api/bye 之后进程自己结束（原来这条分支是只认 Ctrl+C 的死循环）
    port = free_port()
    opened: list[str] = []
    real_launch = hub.launch_shell
    real_open = hub.webbrowser.open
    import core.heal as heal_mod

    real_heal = heal_mod.run_quiet
    hub.launch_shell = lambda url: None            # 模拟"没找到应用窗口浏览器"
    hub.webbrowser.open = lambda url: opened.append(url) or True
    heal_mod.run_quiet = lambda: heal_mod.EXIT_OK

    os.environ["PERSONA_HUB_HOME"] = str(home)
    rc: dict[str, object] = {}
    import threading

    def runner():
        try:
            rc["code"] = hub.main(["--port", str(port)])
        except BaseException as exc:  # noqa: BLE001
            rc["code"] = f"raise:{type(exc).__name__}:{exc}"

    t = threading.Thread(target=runner, daemon=True)
    try:
        t.start()
        ok = False
        deadline = time.time() + 30
        while time.time() < deadline:
            st, _ = http(port, "GET", "/api/ping", timeout=1.0)
            if st == 200:
                ok = True
                break
            if not t.is_alive():
                break
            time.sleep(0.3)
        check("回落到默认浏览器后服务起来了", ok, str(rc))
        check("真的调了 webbrowser.open", bool(opened), str(opened))

        st, d = http(port, "POST", "/api/bye")
        check("页面能发 bye", st == 200, str(d)[:100])
        t.join(timeout=15)
        check("main 自己返回（不是靠 Ctrl+C）", not t.is_alive(), "线程还没结束")
        check("返回码为 0", rc.get("code") == 0, str(rc))
        st_after, _ = http(port, "GET", "/api/ping", timeout=1.0)
        check("端口已释放", st_after == 0, str(st_after))
    finally:
        hub.launch_shell = real_launch
        hub.webbrowser.open = real_open
        heal_mod.run_quiet = real_heal
        os.environ.pop("PERSONA_HUB_HOME", None)


def group_d() -> None:
    print("\n[D] 桌面外壳：✕ 必须真退出（静态判据）")
    src = (ROOT / "desktop" / "src" / "main.js").read_text(encoding="utf-8")
    close_block = src[src.index("win.on('close'"): src.index("win.on('close'") + 400]
    check("✕ 不再隐藏窗口", "win.hide()" not in close_block, close_block[:160])
    check("✕ 里不再 preventDefault", "preventDefault" not in close_block, close_block[:160])
    check("窗口全关后直接退出", "app.on('window-all-closed', () => {\n    stopBackend();\n    app.quit();\n  });" in src,
          "(未找到无条件的 window-all-closed → quit)")
    check("退出前会停后端", "app.on('will-quit'" in src and "stopBackend()" in src)


def group_f() -> None:
    """桌面外壳：固定层必须让开标题栏（静态判据）。

    这条来自一个真事故：抽屉打开后，它的头部标题、刷新按钮、版本号被 Windows
    原生那三个窗口按钮压住一半。原因不是布局写错，而是 `position: fixed` 相对
    视口定位 —— body 上的 padding-top 对它无效，抽屉照样从窗口最上沿铺起。

    判据不用"文件里有没有这行字"，而是把 CSS 解析出来看：选择器覆盖全没覆盖全、
    声明是不是带 !important（不带的压不住 #svc-down 的 inline 样式）、
    以及标题栏高度在两处是不是同一个数。
    """
    print("\n[F] 外壳样式：固定层让开标题栏")
    css = (ROOT / "desktop" / "src" / "shell.css").read_text(encoding="utf-8")
    main_js = (ROOT / "desktop" / "src" / "main.js").read_text(encoding="utf-8")
    web_css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    # 找出所有"选择器里含 drawer-backdrop"的规则块
    import re as _re

    rules = []
    for m in _re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
        sel, body = m.group(1), m.group(2)
        if ".drawer-backdrop" in sel:
            rules.append((" ".join(sel.split()), " ".join(body.split())))
    check("shell.css 里有 drawer-backdrop 的规则", bool(rules), str(len(rules)))

    hit = [r for r in rules if "top" in r[1] and "ph-titlebar" in r[1]]
    check("有一条规则给它让位（top: var(--ph-titlebar)）", bool(hit), str(rules[-1:])[:200])

    if hit:
        sel, body = hit[0]
        need = [".drawer-backdrop", ".modal-backdrop", ".guide-backdrop", ".bye-screen", "#svc-down"]
        miss = [x for x in need if x not in sel]
        check("五个固定层都在同一条规则里", not miss, f"缺 {miss} / 实际 {sel}")
        check("带 !important（否则压不住 #svc-down 的行内样式）", "!important" in body, body[:120])

    # 标题栏高度必须是同一个数：改一处忘另一处，错位会以"文字被遮"的形式回来
    m1 = _re.search(r"--ph-titlebar:\s*(\d+)px", css)
    m2 = _re.search(r"titleBarOverlay:\s*\{[^}]*height:\s*(\d+)", main_js, _re.S)
    check("shell.css 与 main.js 的标题栏高度一致",
          bool(m1) and bool(m2) and m1.group(1) == m2.group(1),
          f"css={m1.group(1) if m1 else '?'} js={m2.group(1) if m2 else '?'}")

    # 这些元素在网页版里必须仍是 fixed：将来若改成普通流式布局，这条让位就成了死规则
    still_fixed = [x for x in (".drawer-backdrop", ".modal-backdrop", ".guide-backdrop")
                   if f"{x} {{" in web_css and "position: fixed" in
                   web_css.split(f"{x} {{")[1].split("}")[0]]
    check("网页版里它们仍是 fixed（否则让位规则失去意义）", len(still_fixed) == 3, str(still_fixed))


def main() -> int:
    print("=" * 56)
    print("单实例 / 关窗即退出")
    print("=" * 56)
    work = Path(tempfile.mkdtemp(prefix="ph-inst-"))
    home = work / "home"
    home.mkdir()
    port = free_port()
    proc = start(port, home)
    try:
        group_a(port)
        group_b(port, home)
        group_d()
        # C 组里有一项要打真实 /api/bye，必须在主服务还活着的时候跑
        group_c(port, home)
        group_e(home)
        group_f()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
