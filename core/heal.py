r"""宿主模板自愈的内核。三个入口共用这一份，避免三份会漂移的真源。

为什么从 `tools/tpl-heal.py` 搬进来
===================================
脚本版只对"开发机上装了 Python 的人"有效。分发出去以后，对方机器上
既没有 Python、也未必有权限建计划任务（本机实测 `schtasks.exe` 被安全策略拦）。
挂在 exe 里的内核就没有这两个前提 —— `persona-hub.exe --heal` 是自足的。

所以定位变了：**自愈不是外挂脚本，是内核能力**，脚本只是开发机上的薄包装。

三条触发路径，覆盖三种现实
--------------------------
    A 打开人格中枢          → 启动时静默跑一次（`hub.py` 里调 `heal()`）
    B 开机                  → `%APPDATA%\...\Startup\persona-hub-heal.cmd`（可选，界面可装可卸）
    C 现在就要              → `persona-hub.exe --heal`

为什么不用计划任务做默认方案
----------------------------
计划任务更准（每 30 分钟），但注册要过 `schtasks.exe` 或 `Register-ScheduledTask`，
两者都可能被对方机器的安全策略拦。开机自启目录**只是往一个文件夹里写个 .cmd**，
纯文件写入，没有权限门槛。准不准是次要的，能不能装上才是第一位的。

判据（任一命中才动手）
----------------------
    state != "patched"     插槽被冲掉
    has_contract == False  尾块被冲掉（人格被挤回中段）
    stale == True          版本号对不上 —— 先试 revalidate，只刷记录不动文件

退出码：0 无需处理 ｜ 1 本次补回 ｜ 2 需要人工 ｜ 3 被锁
"""
from __future__ import annotations

import locale
import os
import sys
import time
from pathlib import Path

from . import tplpatch as T
from .paths import ROOT

EXIT_OK = 0
EXIT_HEALED = 1
EXIT_MANUAL = 2
EXIT_LOCKED = 3

LOCK_STALE_SEC = 600        # 崩溃留下的锁不该把自愈永久卡死

STARTUP_CMD = "persona-hub-heal.cmd"
STARTUP_MARK = "persona-hub 自愈"


# ---------------------------------------------------------------- 日志
def log_path() -> Path:
    p = T.data_root() / "heal.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n"
    try:
        with log_path().open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except OSError:
        pass


# ---------------------------------------------------------------- 锁
def _lock() -> tuple[Path | None, str]:
    """抢锁。返回 (锁路径 或 None, 原因)。

    用 O_EXCL 建文件而不是判断"有没有进程"：后者在 Windows 上要枚举进程，
    既慢又容易被权限挡住。建锁失败（权限、盘只读）**不阻断自愈** ——
    宁可无锁跑一次，也不要因为锁的问题让人格永久停在失效状态。
    """
    lock = T.data_root() / "heal.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > LOCK_STALE_SEC:
            lock.unlink(missing_ok=True)
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return lock, "ok"
    except FileExistsError:
        return None, "locked"
    except OSError as exc:
        return None, f"unlockable:{exc}"


# ---------------------------------------------------------------- 自愈
def heal() -> tuple[int, str]:
    """做一次判断 + 必要时补。返回 (退出码, 说明)。"""
    st = T.status()
    exp = st["expert"]

    if not exp["exists"]:
        return EXIT_MANUAL, f"专家模板不存在：{exp['path']}（宿主结构变了，或没装在工作目录）"

    healthy = exp["state"] == "patched" and exp["has_contract"]

    if healthy and not st["stale"]:
        return EXIT_OK, "已是最新"

    if healthy and st["stale"]:
        # 文件是好的，只是版本号对不上：刷记录，不动文件
        r = T.revalidate()
        if r.get("ok"):
            return EXIT_OK, f"版本号已刷新到 {r.get('appVersion')}（文件未改动）"
        return EXIT_MANUAL, f"版本号对不上且刷新失败：{r.get('reason')}"

    try:
        r = T.patch()
    except T.TplError as exc:
        # 结构变形就停手。这里最忌讳"猜个位置插进去"——
        # 插错位置的模板会让整段身份不渲染，比不补更糟。
        return EXIT_MANUAL, f"补丁中止：{exc}"

    if r.get("patched"):
        T.revalidate()      # 补完顺手对齐版本号，否则下一轮还会判 stale
        return EXIT_HEALED, f"已补回：{r.get('path')}"
    return EXIT_OK, str(r.get("reason") or "无需改动")


def run_quiet() -> int:
    """带锁跑一次，只在需要时写日志。给启动流程和 CLI 共用。"""
    lock, why = _lock()
    if lock is None and why == "locked":
        return EXIT_LOCKED
    if lock is None:
        log(f"warn 未能建锁（{why}），无锁继续")
    try:
        code, msg = heal()
    finally:
        if lock is not None:
            try:
                lock.unlink(missing_ok=True)
            except OSError:
                pass
    if code != EXIT_OK:
        log(f"code={code}  {msg}")
    return code


# ---------------------------------------------------------------- 开机自启
def _launcher() -> tuple[str, str]:
    """返回 (可执行文件, 参数前缀)。

    打包后 `sys.executable` 就是 persona-hub.exe；源码跑则是 python + hub.py。
    两种形态都要能写进 .cmd，所以不能硬编码任一种。
    """
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    return sys.executable, f'"{ROOT / "hub.py"}"'


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def startup_path() -> Path:
    return startup_dir() / STARTUP_CMD


def startup_status() -> dict:
    p = startup_path()
    return {
        "path": str(p),
        "installed": p.is_file(),
        "dir_exists": p.parent.is_dir(),
    }


def install_startup() -> dict:
    """往开机自启目录写一个 .cmd。

    只做文件写入：不碰注册表、不调 schtasks、不用 COM 快捷方式。
    那三样在本机都被安全策略拦过，分发出去更没法指望对方机器放行。
    """
    d = startup_dir()
    if not d.is_dir():
        return {"ok": False, "reason": f"开机自启目录不存在：{d}"}
    exe, pre = _launcher()
    args = f"{pre} --heal --quiet".strip()
    # ASCII only. cmd.exe 按 ANSI 读 .cmd，写中文注释在 GBK 机器上会变乱码；
    # 乱码本身不改行为，但一个"看起来坏掉的文件"会让人不敢删，也就卸不干净。
    body = (
        "@echo off\r\n"
        "rem persona-hub self-heal: re-apply the SOUL slot after a WorkBuddy upgrade.\r\n"
        "rem Generated by persona-hub. Safe to delete this file to disable it.\r\n"
        f'start "" /min "{exe}" {args}\r\n'
    )
    # 编码：cmd.exe 按系统 ANSI 码页读 .cmd。纯 ASCII 路径用 ascii 最稳；
    # 路径里带中文（用户名是中文）时 ascii 会直接抛 UnicodeEncodeError，
    # 那时退回系统首选编码——正是 cmd 用的那个码页。
    for enc in ("ascii", locale.getpreferredencoding(False) or "mbcs"):
        try:
            startup_path().write_text(body, encoding=enc, newline="")
        except UnicodeEncodeError:
            continue
        except OSError as exc:
            return {"ok": False, "reason": f"写入失败：{exc}"}
        return {"ok": True, "path": str(startup_path()), "cmd": f'"{exe}" {args}',
                "encoding": enc}
    return {"ok": False, "reason": f"路径含当前代码页无法表示的文字，写不了 .cmd：{startup_path()}"}


def remove_startup() -> dict:
    p = startup_path()
    if not p.is_file():
        return {"ok": True, "removed": False, "reason": "本来就没装"}
    try:
        p.unlink()
    except OSError as exc:
        return {"ok": False, "reason": f"删除失败：{exc}"}
    return {"ok": True, "removed": True}


def status() -> dict:
    """给界面与 `/api/tpl` 用的只读汇总。"""
    st = T.status()
    return {
        "expert": st["expert"],
        "stale": st["stale"],
        "warnings": st["warnings"],
        "startup": startup_status(),
        "log": str(log_path()),
    }
