"""全局路径与平台常量。

所有落点集中在此，其他模块不得硬编码路径。
数据目录可通过环境变量 PERSONA_HUB_HOME 覆盖（用于隔离测试）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "persona-hub"
APP_NAME_CN = "人格中枢"
VERSION = "1.14.15"

# ---------------------------------------------------------------- 安装根目录
# 两种运行形态要分开处理，否则打包后必然出问题：
#
#   源码运行：资源与数据都在项目目录
#   PyInstaller 单文件：资源被解压到临时目录 sys._MEIPASS（每次启动都可能变），
#                       而 exe 在 sys.executable 旁边 —— 用户数据必须落在 exe 边上，
#                       放临时目录里等于每次启动都清零。
#
# 所以拆成两个根：RESOURCE_ROOT 只读（找 web/ 与 personas/），ROOT 可写（放 data/）。
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
else:
    ROOT = Path(__file__).resolve().parent.parent
    RESOURCE_ROOT = ROOT

# 内置人格模板目录（随程序分发，只读）
PERSONAS_DIR = RESOURCE_ROOT / "personas"
# 前端静态资源
WEB_DIR = RESOURCE_ROOT / "web"


def data_root() -> Path:
    """运行时数据目录。

    优先级：PERSONA_HUB_HOME > <root>/data
    隔离测试时把 PERSONA_HUB_HOME 指向沙盒即可，绝不污染真实 vault。
    """
    env = os.environ.get("PERSONA_HUB_HOME")
    p = Path(env) if env else (ROOT / "data")
    p.mkdir(parents=True, exist_ok=True)
    return p


def vault_dir() -> Path:
    p = data_root() / "vault"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_path() -> Path:
    return data_root() / "hub.log"


def state_path() -> Path:
    return data_root() / "state.json"


# ---------------------------------------------------------------- 用户主目录
HOME = Path(os.path.expanduser("~"))
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))


# 提示词的读取时机**按通道不同**，不能统一说「要重启」：
# 实测 USER.md 是每轮重读的（见 core/server.py 的 LIVE_CHANNELS），
# 其余通道没有实测数据。这里只负责「谁在跑」，不替业务下结论。
_HOST_PROC = {
    "WorkBuddy": ("workbuddy.exe",),
    "DSH": ("deepseek harness.exe", "dsh.exe"),
}


def host_processes() -> dict[str, int]:
    """正在运行的目标客户端，返回 {名称: 进程数}（只含命中的）。

    用途：判断"改动可能看不到"的风险，以及给用户一句可执行的兜底建议。
    **不要**据此直接断言"必须重启"——各通道读取时机不同，实测见 LIVE_CHANNELS。
    """
    import subprocess

    try:
        r = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=8,
        )
        # 中文 Windows 的 tasklist 输出是 GBK，不是 UTF-8
        text = r.stdout.decode("gbk", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return {}

    names = []
    for line in text.splitlines():
        if line.strip():
            names.append(line.split(",")[0].strip('"').lower())

    out: dict[str, int] = {}
    for label, pats in _HOST_PROC.items():
        n = sum(1 for x in names if any(p in x for p in pats))
        if n:
            out[label] = n
    return out


def _chrome_candidates() -> list[Path]:
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    return [
        pf / "Google/Chrome/Application/chrome.exe",
        pf86 / "Google/Chrome/Application/chrome.exe",
        LOCALAPPDATA / "Google/Chrome/Application/chrome.exe",
    ]


def _edge_candidates() -> list[Path]:
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    pf86 = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    return [
        pf86 / "Microsoft/Edge/Application/msedge.exe",
        pf / "Microsoft/Edge/Application/msedge.exe",
        LOCALAPPDATA / "Microsoft/Edge/Application/msedge.exe",
    ]


def browser_exe() -> tuple[Path, str] | None:
    """定位用哪个浏览器当窗口外壳，返回 (路径, 显示名)；都没有则 None。

    为什么优先 Chrome 而不是 Edge（2026-10-30 用户要求，方案 A）：
    Edge 是"系统自带、省事"的选择，但它的进程名、任务栏分组、右键菜单都更像浏览器，
    用起来不像一个独立软件。Chrome 的 `--app` 只是同一套机制换成用户更常用的浏览器，
    零依赖、零体积代价。Edge 保留为兜底：Chrome 没装的机器照样能跑。

    `--app` 的关键权限在于：没有地址栏、没有标签页、独立窗口尺寸，
    关掉窗口就等于退出程序——这些 Chrome 和 Edge 完全一致。
    """
    for c in _chrome_candidates():
        if c.is_file():
            return c, "Chrome"
    for c in _edge_candidates():
        if c.is_file():
            return c, "Edge"
    return None


# ---------------------------------------------------------------- 宿主安装目录
# 补宿主模板（让专家会话也读 SOUL.md）要写 `resources/app.asar.unpacked`，
# 所以必须知道 WorkBuddy 装在哪。装的位置并不固定：本机在 ./
# 别的机器可能在 Program Files 或 LocalAppData\Programs。
#
# 判定标志只看一个：`resources/app.asar.unpacked/resources/templates` 存不存在。
# 目录在就说明这是宿主安装根，别拿 exe 名字猜。
_HOST_CACHE: dict[str, Path | None] = {}


def _host_candidates() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("WORKBUDDY_INSTALL_DIR")
    if env:
        out.append(Path(env))
    # 用户手填的兜底：找不到就自己写一行进去，不用改代码
    hint = data_root() / "host-install-dir.txt"
    try:
        if hint.is_file():
            line = hint.read_text(encoding="utf-8").strip().splitlines()
            if line and line[0].strip():
                out.append(Path(line[0].strip()))
    except OSError:
        pass
    out += [
        LOCALAPPDATA / "Programs" / "WorkBuddy",
        Path(os.environ.get("ProgramFiles", r"C:/Program Files")) / "WorkBuddy",
        Path(os.environ.get("ProgramFiles(x86)", r"C:/Program Files (x86)")) / "WorkBuddy",
        Path(r"./"),
    ]
    return out


def host_install_dir() -> Path:
    """WorkBuddy 安装根。找不到时返回候选里第一个（不存在），调用方按 absent 处理。

    缓存结果：这个函数会被状态检查、补丁、预检反复调用，
    每次都去 stat 一堆路径在慢盘上肉眼可见地卡。
    """
    key = "host"
    if key in _HOST_CACHE and _HOST_CACHE[key] is not None:
        return _HOST_CACHE[key]  # type: ignore[return-value]
    for c in _host_candidates():
        try:
            if (c / "resources" / "app.asar.unpacked" / "resources" / "templates").is_dir():
                _HOST_CACHE[key] = c
                return c
        except OSError:
            continue
    fallback = _host_candidates()[0]
    _HOST_CACHE[key] = None
    return fallback
