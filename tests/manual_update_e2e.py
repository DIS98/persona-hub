"""端到端人工验证：静默覆盖安装 + 装完自动启动。

**不要放进自动化。** 它会真的运行安装包、改注册表、清理目标目录里的文件，
所以必须显式给一个"愿意被覆盖"的沙盒目录。默认不跑。

用法：
    python tests/manual_update_e2e.py --target ./ \\
        --setup desktop\\release\\人格中枢桌面版-Setup-<版本>.exe

判定四条，缺一条算没过（都要有副作用证据，不看"没报错"）：
  1. 装到 --target 指定目录
  2. NSIS 默认目录没有被创建（证明 /D= 真的生效，不是碰巧）
  3. 装完自动启动（--force-run 生效）
  4. 跑起来的是安装包里的版本（shell.log 版本行）

这四条对应 0.3.7 那次事故：装完桌面毫无动静、看着像更新失败。
"""
import argparse
import os
import subprocess
import time
from pathlib import Path


def procs():
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq 人格中枢桌面版.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
    )
    return r.stdout.decode("gbk", "replace").count("人格中枢桌面版.exe")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="沙盒安装目录（会被覆盖，别指真实安装位置）")
    ap.add_argument("--setup", required=True, help="安装包 exe")
    ap.add_argument("--expect-version", default="", help="期望装完运行的版本，如 0.3.8")
    args = ap.parse_args()

    target = Path(args.target)
    setup = Path(args.setup)
    default_dir = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\人格中枢桌面版"))
    shell_log = Path(os.path.expandvars(r"%APPDATA%\人格中枢桌面版\shell.log"))

    for p, what in ((target, "目标目录"), (setup, "安装包")):
        if not p.exists():
            raise SystemExit(f"{what}不存在：{p}")

    print("0) 清场")
    subprocess.run(["taskkill", "/F", "/IM", "人格中枢桌面版.exe"], capture_output=True)
    time.sleep(1)
    print(f"   起始进程数={procs()}  默认目录存在={default_dir.exists()}")

    print(f"1) 静默覆盖安装 -> {target}")
    t0 = time.time()
    r = subprocess.run([str(setup), "/S", "--force-run", "/D=" + str(target)], timeout=180)
    print(f"   安装器 rc={r.returncode} 用时 {time.time()-t0:.1f}s")

    print("2) 等自动启动")
    started = False
    for _ in range(30):
        time.sleep(1)
        if procs() > 0:
            started = True
            break
    print(f"   自动启动={started}  进程数={procs()}")
    time.sleep(6)

    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"   {'✓' if cond else '✗'} {name} {detail}")
        if not cond:
            ok = False

    check("装到了指定目录", (target / "人格中枢桌面版.exe").exists())
    check("默认目录没被创建（/D 生效）", not default_dir.exists(), str(default_dir))
    check("装完自动启动（--force-run 生效）", started)

    ver = "(无 shell.log)"
    if shell_log.exists():
        for line in shell_log.read_text(encoding="utf-8", errors="replace").splitlines()[::-1]:
            if "[外壳] 版本" in line:
                ver = line.strip()
                break
    if args.expect_version:
        check(f"跑起来的是 {args.expect_version}", args.expect_version in ver, ver)
    check("shell.log 可用（主进程日志落盘）", shell_log.exists(), str(shell_log))

    print("\n最终进程数:", procs())
    subprocess.run(["taskkill", "/F", "/IM", "人格中枢桌面版.exe"], capture_output=True)
    print("结果:", "全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
