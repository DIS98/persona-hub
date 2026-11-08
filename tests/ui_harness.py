"""起一个带调试端口的浏览器供 CDP 驱动，并保持存活。

为什么要这个脚本：本机从 shell 后台启动浏览器会立刻退出（headless 与 --app 都一样），
但从 Python 的 subprocess.Popen 启动能常驻——hub.py 打开窗口用的就是这个方式，
已经实测稳定。所以 UI 自动化也走同一条路。

用哪个浏览器跟主程序保持一致（见 core.paths.browser_exe）：Chrome 优先，Edge 兜底。
2026-10-30 之前这里写死 Edge；用户改用 Chrome 之后，写死的那条路会悄悄跑偏。
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import browser_exe  # noqa: E402

_found = browser_exe()
if _found is None:
    raise SystemExit("找不到 Chrome / Edge，无法启动 UI 调试窗口")
BROWSER, SHELL_NAME = _found

PROF = Path(r"./")
URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8799/?noguide=1"
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 300

PROF.mkdir(parents=True, exist_ok=True)

proc = subprocess.Popen(
    [
        str(BROWSER),
        f"--app={URL}",
        "--remote-debugging-port=9222",
        f"--user-data-dir={PROF}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1400,940",
    ],
    close_fds=True,
)
print(f"Edge PID={proc.pid} URL={URL}", flush=True)
print(f"存活 {SECONDS} 秒，期间可连 http://127.0.0.1:9222", flush=True)

try:
    for _ in range(SECONDS):
        time.sleep(1)
        if proc.poll() is not None:
            print(f"Edge 已退出，退出码 {proc.returncode}", flush=True)
            break
finally:
    try:
        proc.terminate()
    except Exception:
        pass
    print("结束", flush=True)
