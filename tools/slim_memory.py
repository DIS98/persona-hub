"""把用户级 MEMORY.md 压到宿主实际能读到的长度内。

背景（实测 2026-10-29）：
  MEMORY.md 写到 25,957 字符，而宿主注入到系统提示词的只有前 ≈4,000 字符。
  也就是说 76% 的内容（网络诊断、开发环境路径、REA 口径……）用户写了但从未生效过。

策略：**不删信息，只分层。**
  - MEMORY.md        → 只留「每轮都要用的判据」，压到 2,000 字符以内，给注入的人格留出预算
  - MEMORY-archive.md → 原文完整快照（含当时的 4 个注入区块），同目录，不会被注入

运行前会先把原文件复制到沙盒，且不覆盖已存在的备份。
用法：python tools/slim_memory.py
"""
from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

MEM = Path.home() / ".workbuddy" / "user-d8ec303f-8536-4540-83cd-1fab0bdbeed2-personal" / "MEMORY.md"
ARCHIVE_NAME = "MEMORY-archive.md"

SLIM = """# 用户级长期规则

> 注入进每轮系统提示词的只有前 ≈4,000 字符（实测），所以这里只放**每次都要用的判据**；
> 事实细节一律进 `MEMORY-archive.md`（同目录，不会被注入）。整理于 2026-10-29。

## 一、三条硬纪律

1. **项目收尾必须回填经验包**：浏览器扩展 → `browser-extension-playbook/`；其他技术域 → 对应
   `<domain>-playbook`（platform-facts / pitfall-catalog / gate-discipline）。只放"去掉项目名还成立"
   的判据，禁止新建平行文档。
2. **验证不能靠"没报错"**：默认失败形态是静默失效。要有副作用证据（产物数据 / 网络到达 / DOM 状态）；
   写完判据必须造一个"看起来满足、实际不起作用"的变异，看它是否报红；报"全通过"前先确认装置本身能跑，
   SKIP 不计入通过。
3. **测试一律在沙盒**：只读随便看，要写就复制一份改，要跑就给独立数据目录。不碰用户正在用的软件与项目源码。

## 二、自用定位

所有自研软件均为自用，不分发不商用 → 不预置合规环节、不拦灰色地带自动化、不做商店材料/付费/遥测。
**唯一硬边界**：规避技术保护措施、未授权访问他人系统、恶意软件、盗版分发 —— 一行明示并不开工。
自用不分发使 GPL 分发义务不触发，但"参考思路"≠"复制源码"，后者仍走 clean-room。

## 三、本机硬限制（会直接拦动作，先按这个选型）

- `reg.exe` / `sc.exe` / `Add-Type` / `New-Object -ComObject` 被安全策略拦 → 用 PowerShell 原生
  cmdlet（`Set-ItemProperty` / `Get-Service` 等），**不要反复重试或换 shell 绕过**。
- Bash 里禁止调 `powershell.exe` → 系统运维一律走 PowerShell 工具。
- **Edge 带 `--remote-debugging-port` 会立即退出**（exit 0，1 秒内）→ 本机做不了浏览器自动化
  （CDP / Playwright 别装）。`--screenshot` 可用，`--dump-dom` 静默失效。
  替代打法：页面加 `?only=<区块>` 直达参数逐屏截图；交互靠 HTTP 端到端测试。
- **进程级 `HTTP_PROXY` 被注入**为 `http://127.0.0.1:<随机端口>` → 测带宽/连通前先 pop 掉；
  唯一可信的是网卡层字节计数。
- 版本与系统事实以**实际执行与 `winver`** 为准，不看注册表键值（`ProductName` 会把 Win11 报成 Win10）。

## 四、改完即同步

任何交付物改动 → 立刻同步三件：产物自报版本 == 真源版本、README 三层、分享链接
（用户明确要求"每次修改都及时上传"）。

## 五、其余规则在哪

GitHub 认证、专家包维护两个坑、REA 逆向口径、本机开发环境路径、网络诊断数据、
第三方提示词包接入口径、安卓环境 —— 全部在 **`MEMORY-archive.md`**，需要时读那份。
"""


def main() -> int:
    if not MEM.is_file():
        print(f"找不到 {MEM}")
        return 1

    raw = MEM.read_text(encoding="utf-8")
    before = len(raw)
    digest = hashlib.sha256(MEM.read_bytes()).hexdigest()
    print(f"原文件 {before} 字符 | sha256 {digest[:16]}")

    # 1) 沙盒备份（不覆盖已有备份）
    stamp = time.strftime("%Y%m%d-%H%M%S")
    sandbox = Path(r"./") / f"memory-slim-{stamp}"
    sandbox.mkdir(parents=True, exist_ok=True)
    backup = sandbox / "MEMORY.md.orig"
    shutil.copy2(MEM, backup)
    print(f"备份 → {backup}")

    # 2) 归档：原文完整快照，前面加一段说明
    archive = MEM.parent / ARCHIVE_NAME
    header = (
        "# 用户级规则归档（不会被注入）\n\n"
        f"> 这是 2026-10-29 瘦身前的完整快照，原文 {before} 字符。\n"
        "> 为什么归档：宿主对 MEMORY.md 只注入前 ≈4,000 字符，超出部分从未生效过。\n"
        "> 瘦身后的 MEMORY.md 只保留高频判据，细节留在这里，需要时按需查阅。\n\n"
        "---\n\n"
    )
    if archive.exists():
        archive.unlink()
    archive.write_text(header + raw, encoding="utf-8", newline="\n")
    print(f"归档 → {archive}（{len(header) + before} 字符）")

    # 3) 写新 MEMORY.md
    MEM.write_text(SLIM, encoding="utf-8", newline="\n")
    after = len(SLIM)
    print(f"新 MEMORY.md {after} 字符（压缩 {before - after}，剩 {(after / before) * 100:.1f}%）")
    print(f"给人格留下的预算：约 {4000 - after} 字符")

    if after > 4000:
        print("[警告] 仍超过 4000 字符，需要继续压缩")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
