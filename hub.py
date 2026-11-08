"""人格中枢 —— 入口。

用法：
    python hub.py                  启动 GUI（默认）
    python hub.py --no-gui         只起服务，打印地址，适合调试
    python hub.py --port 8900      指定起始端口

GUI 外壳用 Chrome / Edge 的 `--app` 模式：无地址栏、无标签页，看起来就是一个独立应用。
优先 Chrome（用户更常用、任务栏分组更像独立软件），Edge 兜底（系统预装，没装 Chrome 的机器照样能跑）。
两者都不需要打包 WebView2 运行时，也不需要引入第三方 GUI 库。
若两个都找不到，退回默认浏览器打开——功能完全一样，只是多了地址栏。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser

# hub.log 的单份上限。与外壳的 shell.log（512KB）同一量级：
# 排查要的是"最近这一段"，不是全部历史。
_LOG_KEEP = 1024 * 1024
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---------------------------------------------------------------- 更新引导
# 必须排在加载 core.server / core.adapters 之前：
# 那时 Python 只加载了 core.paths 与 core.update 两个轻量模块，
# 磁盘上的其余 .py 还是旧内容，覆盖是安全的；覆盖完立刻重启，
# 所有模块都用新代码重新加载，不会出现「新旧混用」。
try:
    from core.paths import ROOT as _ROOT
    from core.update import apply_pending as _apply_pending

    _applied = _apply_pending(_ROOT)
    if _applied:
        _say(f"[更新] {_applied}，正在重启以启用新版本…")
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as _exc:  # noqa: BLE001
            _say(f"[更新] 重启失败，本次仍用旧版本运行：{_exc}")
except Exception as _exc:  # noqa: BLE001
    _say(f"[更新] 检查待应用更新时出错（已忽略，不影响启动）：{_exc}")

from core.paths import APP_NAME_CN, VERSION, browser_exe, data_root
from core.server import DEFAULT_PORT, Server


# 无页面活动多久后认为窗口已关（秒）。
#
# 这个判据本身就是将就 —— "没有 HTTP 请求"不等于"窗口关了"，它只是个代理指标。
# 2026-11-01 一整天栽在这里：日志里 16:29:15 写着「30 分钟无页面活动」退出，
# 而用户 17:10 还在点那个窗口，页签上写「统计失败：Failed to fetch」。
#
# 原因：前端心跳是 5 秒一次，但 Chrome 的 Memory Saver 会把长时间后台的标签
# 冻结甚至丢弃，setInterval 整个不跑 —— 心跳停了，窗口还在。
# 30 分钟阈值于是成了"正常使用中被掐断"的开关。
#
# 所以：主判据交给 /api/bye（页面卸载时 sendBeacon，这是真信号），
# 这里只做兜底，且必须远大于浏览器冻结标签的时间尺度。12 小时足够兜住
# "窗口关了但 beacon 没发出去"的残留进程，又不会在用户还在用的时候动手。
IDLE_LIMIT_SECONDS = 12 * 3600


def _browser_profile() -> Path:
    """独立的浏览器 profile。

    不共用用户日常浏览器配置：一是不会往他的历史记录里塞记录，
    二是避免与他已登录的账号状态互相干扰。

    刻意用**固定目录**而不是每次新建：早先试过每次 run-<pid> 建新目录再删旧的，
    结果启动路径里出现一次 481 个文件的递归删除，既慢又会触发主机的批量删除保护，
    直接把启动打断。启动路径上不做删除动作。
    """
    p = data_root() / "browser-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def launch_shell(url: str) -> tuple[subprocess.Popen, str] | None:
    """拉起无地址栏的应用窗口，返回 (进程句柄, 浏览器名)；没有可用浏览器则 None。"""
    found = browser_exe()
    if found is None:
        return None
    exe, label = found
    args = [
        str(exe),
        f"--app={url}",
        f"--user-data-dir={_browser_profile()}",
        "--window-size=1280,880",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
    ]
    try:
        return subprocess.Popen(args, close_fds=True), label
    except OSError:
        return None


def _say(text: str) -> None:
    """给 `--heal*` 这类命令行用：打包成 --windowed 时没有控制台。

    PyInstaller 窗口模式下 sys.stdout 可能是 None，直接 print 会抛
    AttributeError，退出码就变成非 0 —— 计划任务会误判成失败。
    所以这里先判空，再写一行到 hub.log，保证"跑过"有痕迹。
    """
    _log(text)
    try:
        if sys.stdout is not None:
            print(text)
    except Exception:  # noqa: BLE001
        pass


def _log(line: str) -> None:
    """把启动/退出这类「只在事后才想知道」的事件记进 hub.log。

    为什么单独加这一条：之前日志里只有写文件的事务记录，既看不出启动过几次，
    也看不出每次为什么退出。用户说「我看到有三个窗口开着」，翻日志一个字都查不到。

    轮转：这份日志只增不减，而每次启动、每次注入、每次报错都往里写。
    外壳那份 shell.log 有 512KB 上限，内核这份以前没有——用一年之后
    它会变成几十兆，用户想"把日志发出来"时会卡在找文件上。
    超过上限就滚成 .1，只留一代：排查要的是最近这一段。
    """
    try:
        from core.paths import log_path

        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            if p.stat().st_size > _LOG_KEEP:
                p.replace(p.with_suffix(p.suffix + ".1"))
        except OSError:
            pass   # 第一次启动没有这个文件，正常
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"[{ts}] {line}\n")
    except Exception:  # noqa: BLE001
        pass


def _probe(port: int) -> dict | None:
    """问一下这个端口上是不是本程序。是就返回 ping 的数据，否则 None。

    判据用 ping 里的 app 字段，不用「能不能解析出 JSON」：
    端口被别的服务占着时，那种判法会把它当成自己的实例。
    显式清空代理处理器——本机进程级 HTTP_PROXY 会把 127.0.0.1 也代理走。
    """
    import json
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/ping", timeout=0.6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if data.get("ok") and (data.get("data") or {}).get("app") == APP_NAME_CN:
        return data.get("data") or {}
    return None


def _find_running_instance(start_port: int, span: int = 20) -> int | None:
    """找本机已经在跑的『人格中枢』，返回端口；没有则 None。

    为什么必须有这道守卫：`Server.start()` 在端口被占时会往后找空位，
    所以双击两次就有两个服务、两个窗口——而**两个实例都在改同一批配置文件**。
    用户看到的是「怎么开了三个窗口」，真正的风险是并发写同一份 USER.md。
    """
    for p in range(start_port, start_port + span):
        if _probe(p) is not None:
            return p
    return None


def _watch_until_closed(srv, port: int, idle_limit: float) -> str:
    """等页面消失。返回退出原因，供日志用。

    抽成函数是因为有两个入口要用同一套判据（找到 Chrome/Edge 的应用窗口、回落到默认浏览器），
    两处各写一份必然漂移 —— 回落那处原来就漏了 /api/bye，关掉页签后进程一直挂着。

    判活不看 Popen 的 PID：浏览器启动器把活交给 browser 进程后会自己退出，
    拿那个 PID 判断"窗口还开着"必然是错的。只认两条信号：
      1) /api/bye：页面卸载 sendBeacon，或用户点界面上的「退出」——立即生效
      2) 长期无请求兜底（IDLE_LIMIT_SECONDS），兜住"bye 没发出去"的残留
    """
    reason = "未知"
    try:
        while True:
            time.sleep(1)
            if srv.app is not None and srv.app.closing:
                reason = "页面关闭(/api/bye)"
                break
            if (time.time() - srv.last_request) > idle_limit:
                reason = f"{int(idle_limit) // 60} 分钟无页面活动"
                _say(f"已 {int(idle_limit) // 60} 分钟无页面活动，视为窗口已关。")
                break
    except KeyboardInterrupt:
        reason = "Ctrl+C"
    return reason


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hub", description=f"{APP_NAME_CN} v{VERSION}")
    ap.add_argument("--no-gui", action="store_true", help="只启动服务，不打开窗口")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"起始端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--heal", action="store_true", help="补回被宿主升级覆盖的模板后退出，不开界面")
    ap.add_argument("--heal-status", action="store_true", help="打印模板与自启状态后退出")
    ap.add_argument("--install-startup", action="store_true", help="装开机自检（往启动目录写个 cmd）")
    ap.add_argument("--remove-startup", action="store_true", help="卸开机自检")
    # 兼容脚本版与 .cmd 里的写法。`run_quiet()` 本来就不打印，
    # 但少了这个参数，argparse 会以退出码 2 中止 —— 开机脚本于是"成功地失败了"。
    ap.add_argument("--quiet", action="store_true", help="无控制台输出（自愈命令用）")
    args = ap.parse_args(argv)

    # 这四条是命令，不是启动参数：处理完就退，不碰端口、不起服务。
    # 为什么要放进 exe：分发出去以后对方机器上可能没有 Python，
    # 自愈必须能靠 `persona-hub.exe --heal` 独立跑起来。
    if args.heal or args.heal_status or args.install_startup or args.remove_startup:
        from core import heal as _heal

        if args.heal_status:
            st = _heal.status()
            exp = st["expert"]
            _say(f"state={exp['state']} soul={exp['has_soul_slot']} "
                 f"contract={exp['has_contract']} stale={st['stale']}")
            _say(f"startup={'on' if st['startup']['installed'] else 'off'}  "
                 f"{st['startup']['path']}")
            for w in st["warnings"]:
                _say(f"warn: {w}")
            return _heal.EXIT_OK
        if args.install_startup:
            r = _heal.install_startup()
            _say(str(r))
            return _heal.EXIT_OK if r.get("ok") else _heal.EXIT_MANUAL
        if args.remove_startup:
            r = _heal.remove_startup()
            _say(str(r))
            return _heal.EXIT_OK if r.get("ok") else _heal.EXIT_MANUAL
        return _heal.run_quiet()

    _say(f"{APP_NAME_CN} v{VERSION}")
    _say(f"数据目录: {data_root()}")

    # 单实例守卫：已有实例就只开窗口，不再起第二个服务。
    # 只对 GUI 生效——`--no-gui` 是明确的「我要跑一个服务」意图，测试也依赖它。
    if not args.no_gui:
        running = _find_running_instance(args.port)
        if running is not None:
            # 已有实例，还要判断"窗口是不是已经开着"。
            # 只判断"后端在不在"是不够的：双击第二次时后端确实在跑，可窗口也开着，
            # 再 launch_shell 一次就多出一个标签页 —— 用户看到的还是"开了两个"。
            info = _probe(running) or {}
            if info.get("page_alive"):
                _say(f"已在运行（端口 {running}），窗口还开着，不再开第二个。")
                _log(f"[复用] v{VERSION} port={running} 页面在线，未开新窗口 pid={os.getpid()}")
                return 0
            _say(f"已在运行（端口 {running}），后端在、没有窗口，补开一个。")
            _log(f"[复用] v{VERSION} port={running} 页面不在，补开窗口 pid={os.getpid()}")
            launch_shell(f"http://127.0.0.1:{running}/")
            return 0

    # 启动自愈：宿主升级会覆盖模板，插槽与契约尾块一起消失且不报错。
    # 放在起服务之前跑，界面上看到的就是修好之后的状态，用户不必点任何东西。
    # 只走 GUI 路径 —— `--no-gui` 是测试与调试入口，不该有"写宿主目录"的副作用。
    if not args.no_gui:
        try:
            from core import heal as _heal

            _code = _heal.run_quiet()
            if _code not in (_heal.EXIT_OK, _heal.EXIT_LOCKED):
                _log(f"[自愈] code={_code} 见 {_heal.log_path()}")
        except Exception as exc:  # noqa: BLE001
            _log(f"[自愈] 跳过：{exc}")

    srv = Server(args.port)
    try:
        port = srv.start()
    except RuntimeError as exc:
        _say(f"[错误] {exc}")
        _log(f"[启动失败] {exc}")
        return 1

    url = f"http://127.0.0.1:{port}/"
    _say(f"服务已就绪: {url}")

    if args.no_gui:
        _log(f"[启动] v{VERSION} port={port} pid={os.getpid()} 模式=no-gui")
        _say("按 Ctrl+C 或页面发 /api/bye 退出")
        # 和下面两条前台分支共用同一套判活。
        # 原来这里是 `while True: sleep(1)`，只认 Ctrl+C —— 同一个「退出」按钮
        # 在 GUI 模式下有效、在 no-gui 模式下毫无反应。而桌面外壳正是用 no-gui 起后端的，
        # 用户点界面上的「退出」会以为退了，后端其实还在，下次启动又撞上"已在运行"。
        reason = "异常退出"
        try:
            reason = _watch_until_closed(srv, port, IDLE_LIMIT_SECONDS)
        finally:
            srv.stop()
            _log(f"[退出] port={port} 原因={reason}(no-gui)")
        return 0

    # launch_shell 找不到浏览器时返回 None，直接解包会抛 TypeError —— 
    # 打包成 --windowed 后没有控制台，表现就是"双击没反应"，而且下面的
    # "回落默认浏览器"分支永远走不到。这里显式兜住。
    shell = launch_shell(url)
    proc, shell_name = shell if shell else (None, "")
    if proc is not None:
        _log(f"[启动] v{VERSION} port={port} pid={os.getpid()} 窗口已拉起（{shell_name}）")
        _say(f"窗口已打开（{shell_name} 应用窗口模式）。关窗口或点界面右上角「退出」都会结束本进程。")
        try:
            reason = _watch_until_closed(srv, port, IDLE_LIMIT_SECONDS)
        finally:
            srv.stop()
            _log(f"[退出] port={port} 原因={reason}")
        _say("退出。")
        return 0

    _log(f"[启动] v{VERSION} port={port} pid={os.getpid()} 未找到 Chrome / Edge，改用默认浏览器")
    _say("未找到 Chrome / Edge，改用默认浏览器打开（关掉页签即退出）。")
    webbrowser.open(url)
    # 这里原来是个死循环，只认 Ctrl+C：关掉页签后进程一直挂着，用户以为关了、
    # 其实后端还在，下次双击又撞上"已在运行"。和 GUI 分支共用同一套判活。
    try:
        reason = _watch_until_closed(srv, port, IDLE_LIMIT_SECONDS)
    finally:
        srv.stop()
        _log(f"[退出] port={port} 原因={reason}(默认浏览器模式)")
    _say("退出。")
    return 0


_last_seen = time.time()


def _report_fatal(tb: str) -> None:
    """打包成 --windowed 后没有控制台，异常必须想办法让人看见。

    两条路都走：写进 data/hub.log，以及弹一个系统消息框。
    只写日志等于没写——用户不会没事去看日志文件。
    """
    try:
        from core.paths import log_path

        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("\n==== 启动失败 ====\n" + tb + "\n")
    except Exception:  # noqa: BLE001
        pass

    try:
        import ctypes

        tail = tb.strip().splitlines()[-12:]
        ctypes.windll.user32.MessageBoxW(
            0,
            "启动失败，错误摘要：\n\n" + "\n".join(tail) + "\n\n完整信息见 data\\hub.log",
            "人格中枢 · 启动失败",
            0x10,  # MB_ICONERROR
        )
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        import traceback

        _report_fatal(traceback.format_exc())
        raise
