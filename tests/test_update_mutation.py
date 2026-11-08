#!/usr/bin/env python3
"""更新链路的变异测试：把每条修复改回去，看 test_updater.js 会不会报红。

为什么单列一个文件：更新是唯一一条"自己替换自己"的路径，改坏之后用户不会有报错，
只会永远停在旧版本。所以"测试通过"必须能证明"测试抓得到错"——
每条修复都对应一条变异，删掉它必须报红，否则那条断言就是摆设。

原则：所有写回都带 newline=""，否则 Windows 会把源码转成 CRLF，
下次按多行锚点定位会全部失配。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "desktop" / "src" / "updater.js"
TEST = ROOT / "tests" / "test_updater.js"

MUTATIONS = [
    (
        "M1 去掉写流的 error 监听",
        "    out.on('error', (e) => fail(new Error(`写文件失败（${tmp}）：${e.message}`)));\n",
        "    /* 变异：不监听写流错误 */\n",
    ),
    (
        "M2 去掉 aborted 监听（断线后永久悬挂）",
        "      res.on('aborted', () => fail(new Error(`下载被中断：连接在 ${got} 字节处断开`)));\n",
        "      /* 变异：不监听 aborted */\n",
    ),
    (
        "M3 去掉 windowsVerbatimArguments（含空格路径被加引号）",
        "    // 见上面的实测：不加这句，含空格的路径会被 Node 加上引号\n    windowsVerbatimArguments: true,\n",
        "",
    ),
    (
        "M4 不挂安装器事件回调（应用先退出、用户啥都看不到）",
        "  if (typeof onEvent === 'function') {\n"
        "    child.on('spawn', () => onEvent(null));\n"
        "    child.on('error', (e) => onEvent(e));\n"
        "  }\n",
        "",
    ),
    (
        "M5 verifyFile 不校验 sha（坏包也当好的用）",
        "    return h.digest('hex') === expectSha;",
        "    return true; /* 变异：不比 sha */",
    ),
    (
        "M6 过期安装包判据失效（temp 永远清不掉，每版堆 116 MB）",
        "    const cmp = compareVer(m[1], currentVersion);\n"
        "    if (cmp !== null && cmp < 0) out.push(name);",
        "    out.push(name); /* 变异：不判版本，见一个删一个 */",
    ),
    (
        "M7 版本读不出来也照删（判据不成立时动别人的文件）",
        "  if (!parseVer(currentVersion)) return [];\n",
        "",
    ),
]


def run_tests() -> tuple[int, str]:
    r = subprocess.run(
        ["node", str(TEST)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    out = (r.stdout or "") + (r.stderr or "")
    line = next((ln.strip() for ln in reversed(out.splitlines()) if "通过" in ln), "")
    return r.returncode, line or out[-200:]


def main() -> int:
    original = SRC.read_text(encoding="utf-8", newline="")
    code, line = run_tests()
    print(f"  基线：{line}")
    if code != 0:
        print("  基线本身就不过，变异无意义")
        return 1

    missed = []
    for name, old, new in MUTATIONS:
        if old not in original:
            print(f"  ✗ {name}：锚点没命中（源码改过？必须同步这条变异）")
            missed.append(name)
            continue
        SRC.write_text(original.replace(old, new, 1), encoding="utf-8", newline="")
        try:
            bad_code, bad_line = run_tests()
        finally:
            SRC.write_text(original, encoding="utf-8", newline="")
        if bad_code == 0:
            print(f"  ✗ {name}：没报红 —— 这条断言抓不到错")
            missed.append(name)
        else:
            print(f"  ✓ {name}：报红")

    # 恢复后再跑一次，确认没留下残迹
    code, line = run_tests()
    if code != 0:
        print("  恢复后测试不过，源码可能没还原干净")
        return 1

    total = len(MUTATIONS)
    print(f"  抓到错 {total - len(missed)}/{total}；问题：{'、'.join(missed) if missed else '无'}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
