"""变异测试：证明 `test_hardening.py` 里的判据**真的能抓到错**。

为什么需要这一层
================
`test_hardening` 全绿只说明"现在是对的"，不说明"下次改坏了会被发现"。
本脚本把每条修复逐一改回去（注入变异），跑一遍 `test_hardening`，
**每一轮都必须报红**；有一轮仍绿，就说明那条判据是摆设。

做法：备份原文件字节 → 字符串替换 → 跑子进程 → 恢复 → 核对哈希。
任何一环出错（含 Ctrl+C）都在 finally 里还原，不会把变异留在源码里。

用法：
    python tests/test_hardening_mutation.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = "tests/test_hardening.py"
TPL_TARGET = "tests/test_tpl.py"

# (编号, 文件, 原文, 变异后, 预期哪一条会红)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    ("M1", "core/adapters/base.py",
     "    normalized = neutralize_markers(inline)",
     "    normalized = inline",
     "H1 正文标记未被中和"),
    # 这条有两道防线（先 slug + 再兜底），单拆任何一道另一道仍然拦得住，
    # 所以变异必须还原成「出 bug 的那一版」整体，才有意义。
    ("M2", "core/persona.py",
     '        new_id = re.sub(r"[^A-Za-z0-9_\\-]", "-", str(pid or src.stem)).strip("-").lower()\n'
     '        if not re.fullmatch(r"[a-z0-9_\\-]{1,48}", new_id):',
     '        new_id = pid or src.stem\n'
     '        if False:',
     "H2 导入 id 穿越"),
    ("M3", "core/server.py",
     "        if host_only and host_only.strip().lower() not in ALLOWED_HOSTS:",
     "        if False:",
     "H3 外域 Host 放行"),
    ("M4", "core/server.py",
     "    @serialized\n    def apply(self, payload: dict) -> dict:",
     "    def apply(self, payload: dict) -> dict:",
     "H4 并发丢更新"),
    ("M5", "core/server.py",
     '        name = clean_name(payload.get("name"))',
     '        name = str(payload.get("name") or "").strip()',
     "H5 名称换行截断"),
    ("M6", "core/server.py",
     "MAX_BODY_BYTES = 16 * 1024 * 1024",
     "MAX_BODY_BYTES = 16 * 1024 * 1024 * 1024",
     "H6 请求体无上限"),
    ("M7", "core/server.py",
     '        self.send_header("X-Content-Type-Options", "nosniff")',
     '        self.send_header("Access-Control-Allow-Origin", "null")\n'
     '        self.send_header("X-Content-Type-Options", "nosniff")',
     "H7 ACAO: null 放行"),
    ("M8", "core/server.py",
     '        if not self.library.exists(pid):\n'
     '            # 注释一直写着"映射成 404"，实际走的是 PersonaError → 422。\n'
     '            # 语义上"要删的东西不存在"是 404，前端按状态码分支会走错。\n'
     '            raise ApiError("PERSONA_NOT_FOUND", f"人格不存在：{pid}", 404)\n',
     "",
     "H8 删除不存在返回 404"),
    ("M9", "core/adapters/__init__.py",
     "    for p in clean:\n"
     "        if Path(p).is_dir():\n"
     '            raise ValueError(f"这是目录，不是文件：{p}（请填具体到 .md 的文件路径）")\n',
     "",
     "H9 目录目标添加时拒绝"),
    # 宿主模板插槽：把"有没有插槽"的判定改成恒真，test_tpl 必须报红。
    # 这一条保护的正是"界面显示已补、其实模板里没有"这种情况。
    ("M10", "core/tplpatch.py",
     '        "has_soul_slot": bool(SOUL_SLOT_RE.search(text)),',
     '        "has_soul_slot": True,',
     "模板插槽判定（TL-1 判据）", TPL_TARGET),
    # 下面两条来自"探针升级为判据"：原先 probe_adversarial.py 只打印不判定，
    # 所以这两处一旦退化，谁也不会知道。
    ("M11", "core/server.py",
     """        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._err("FORBIDDEN", "非法路径", 403)""",
     """        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            pass""",
     "H10 静态目录穿越"),
    ("M12", "core/txn.py",
     """            if mode_before is not None:
                try:
                    os.chmod(p, mode_before)""",
     """            if False:
                try:
                    os.chmod(p, mode_before)""",
     "H12 只读属性还原"),
    # 下面两条守的是"关得掉、不会开两个"，用 test_instance 那套判据验
    ("M13", "hub.py",
     '            if info.get("page_alive"):',
     "            if False:",
     "单实例：窗口在线时不许再开窗", "tests/test_instance.py"),
    ("M14", "desktop/src/main.js",
     """  win.on('close', () => {
    saveBounds();
    quitting = true;
  });""",
     """  win.on('close', (e) => {
    if (!quitting) {
      e.preventDefault();
      win.hide();
    } else {
      saveBounds();
    }
  });""",
     "外壳：✕ 必须真退出", "tests/test_instance.py"),
    ("M15", "hub.py",
     "    shell = launch_shell(url)" + chr(10) + '    proc, shell_name = shell if shell else (None, "")',
     "    proc, shell_name = launch_shell(url)",
     "找不到浏览器时不许崩", "tests/test_instance.py"),
    ("M16", "desktop/src/shell.css",
     ".drawer-backdrop,\n.modal-backdrop,\n.guide-backdrop,\n.bye-screen,\n#svc-down {\n  top: var(--ph-titlebar) !important;\n}",
     ".drawer-backdrop,\n.modal-backdrop,\n.guide-backdrop,\n.bye-screen,\n#svc-down {\n}",
     "外壳：固定层让开标题栏", "tests/test_instance.py"),
    # 更新通道：漏了 /D= 就会装到默认目录，用户自选了安装位置时等于装出第二份
    ("M17", "desktop/src/updater.js",
     "  if (installDir) args.push('/D=' + installDir);",
     "  if (false) args.push('/D=' + installDir);",
     "更新：静默安装要带 /D=当前目录", "tests/test_updater.js"),
    # 请求体不 drain：大 body 打到未匹配路由时客户端拿到连接中断而不是 404
    ("M18", "core/server.py",
     "        try:" + chr(10) + "            self.rfile.read(n)" + chr(10) + "        except (OSError, ValueError):",
     "        try:" + chr(10) + "            pass" + chr(10) + "        except (OSError, ValueError):",
     "H13 未匹配路由也要回 JSON", "tests/test_hardening.py"),
]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def run_suite(target: str = TARGET) -> tuple[int, str]:
    # 外壳那部分是 JS（updater.js），判据只能由 node 跑。
    # 统一在这里分派，而不是给 JS 单开一套变异流程 —— 两套流程必然漂移。
    cmd = ["node", target] if target.endswith(".js") else [sys.executable, target]
    try:
        out = subprocess.run(cmd, cwd=str(ROOT),
                             capture_output=True, text=True, timeout=900)
        return out.returncode, (out.stdout or "") + (out.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "超时"


def main() -> int:
    print("=" * 62)
    print("变异测试：每条判据必须能抓到错")
    print("=" * 62)

    # 基座：未变异时必须全绿，否则后面"报红"说明不了任何事
    print("\n[基座] 未变异跑一次")
    rc, out = run_suite()
    if rc != 0:
        print("  !! 基座就没通过，以下结论无效。先修好再跑。")
        for line in out.splitlines():
            if "\u2717" in line:
                print("   ", line.strip())
        return 2
    print("  ✓ 基座全绿（26 项），可以开始变异")

    ok = 0
    bad: list[str] = []
    for item in MUTATIONS:
        mid, rel, old, new, expect = item[:5]
        suite = item[5] if len(item) > 5 else TARGET
        p = ROOT / rel
        origin = p.read_bytes()
        before = sha(origin)
        text = origin.decode("utf-8")
        if old not in text:
            print(f"\n[{mid}] 找不到变异锚点，跳过 —— 源码改过了，按新代码更新本脚本")
            bad.append(f"{mid} 锚点失效")
            continue
        if text.count(old) > 1:
            print(f"\n[{mid}] 锚点不唯一（{text.count(old)} 处），跳过")
            bad.append(f"{mid} 锚点不唯一")
            continue

        print(f"\n[{mid}] 注入变异：{expect}")
        try:
            p.write_bytes(text.replace(old, new, 1).encode("utf-8"))
            t0 = time.time()
            rc, out = run_suite(suite)
            cost = time.time() - t0
            if rc == 0:
                print(f"  \u2717 变异后仍然全绿 —— 这条判据抓不到错（{cost:.0f}s）")
                bad.append(f"{mid} 判据抓不到错")
            else:
                hit = [ln.strip() for ln in out.splitlines() if "\u2717" in ln]
                print(f"  ✓ 报红（{cost:.0f}s）：{hit[0] if hit else '失败但无明细'}")
                ok += 1
        finally:
            p.write_bytes(origin)
            if sha(p.read_bytes()) != before:
                print(f"  !! 恢复失败，请手工还原 {rel}")
                return 3

    print("\n" + "=" * 62)
    print(f"抓到错 {ok}/{len(MUTATIONS)}；问题：{bad or '无'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
