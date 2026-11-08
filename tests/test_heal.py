"""自愈脚本：在沙盒宿主里跑，证明它"该补的时候补、不该补的时候不动手"。

为什么必须在沙盒里测
====================
这个脚本要做的事是**写宿主安装目录**。拿真实宿主测，等于每跑一次用例就动一次
用户正在用的软件 —— 一次失败就是人格消失且没有任何报错。
所以这里造一个假宿主（目录结构照抄真实布局），把 `host_install_dir` / `data_root`
两个入口换掉，跑完连沙盒一起删。

四类样本，每条都对应一个真实会发生的升级形态
--------------------------------------------
    A 全新升级    插槽与尾块都没了        → 必须补回，且补完是完整状态
    B 已是最新    什么都没变              → 必须一个字节都不动
    C 半残        插槽在、尾块被冲掉      → 只补尾块
    D 结构变形    锚点没了                → 必须停手、不写文件、退出码 2
    E 版本号漂移  文件是好的、版本对不上  → 只刷记录，模板内容不变

用法
----
    python tests/test_heal.py
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import tplpatch as T  # noqa: E402

PASS = 0
FAIL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
        return True
    FAIL += 1
    FAILS.append(f"{name}  {detail}")
    print(f"  \u2717 {name}  -> {detail}")
    return False


def load_heal():
    """自愈逻辑在内核里（`core/heal.py`），不在脚本里。

    分工理由：脚本只对装了 Python 的机器有效，内核跟着 exe 走 ——
    所以真正被测的必须是内核这一份，否则测的是一个分发出去不存在的文件。
    """
    from core import heal as H
    return H


# ---------------------------------------------------------------- 沙盒
class Sandbox:
    """假宿主。目录层级与真实安装一致，锚点文件来自真实原档备份。"""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ph-heal-"))
        self.host = self.root / "host"
        self.data = self.root / "data"
        self.tpl_dir = (self.host / "resources" / "app.asar.unpacked"
                        / "resources" / "templates")
        self.tpl_dir.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.tpl = self.tpl_dir / T.EXPERT_TPL
        self._pristine = self._from_backup()
        self._saved = (T.host_install_dir, T.data_root)
        T.host_install_dir = lambda: self.host          # type: ignore[assignment]
        T.data_root = lambda: self.data                 # type: ignore[assignment]

    def _from_backup(self) -> bytes:
        """取真实原档（无插槽版）。找不到就退化成最小可用模板。"""
        for p in sorted((ROOT / "data" / "tpl-backup").glob(f"{T.EXPERT_TPL}.*.orig")):
            return p.read_bytes()
        return (
            "<identity_context>\r\n{% if WorkspaceIdentityMode %}\r\n"
            "If SOUL.md is present, embody its persona and tone.\r\n"
            "Stay consistent with the latest injected user profile.\r\n"
            "## USER.md\r\nPath: {{ UserPath }}\r\n"
            "{% if UserContent %}{{ UserContent | trim }}{% endif %}\r\n"
            "</identity_context>\r\n"
        ).encode("utf-8")

    def seed(self, content: bytes | None = None, version: str = "5.9.3") -> None:
        self.tpl.write_bytes(content if content is not None else self._pristine)
        mf = self.host / "resources" / "install-manifest.json"
        mf.write_text(json.dumps({"appVersion": version}, ensure_ascii=False), encoding="utf-8")

    def text(self) -> str:
        return self.tpl.read_text(encoding="utf-8", errors="replace")

    def set_version(self, version: str) -> None:
        mf = self.host / "resources" / "install-manifest.json"
        mf.write_text(json.dumps({"appVersion": version}, ensure_ascii=False), encoding="utf-8")

    def close(self) -> None:
        T.host_install_dir, T.data_root = self._saved  # type: ignore[assignment]
        shutil.rmtree(self.root, ignore_errors=True)


def main() -> int:
    heal_mod = load_heal()

    print("\n[A] 全新升级：插槽与尾块都没了")
    sb = Sandbox()
    try:
        sb.seed()
        check("样本本身没有插槽", not T.SOUL_SLOT_RE.search(sb.text()))
        code, msg = heal_mod.heal()
        check("退出码 = 1（补回）", code == 1, f"{code} {msg}")
        t = sb.text()
        check("补完有插槽", bool(T.SOUL_SLOT_RE.search(t)))
        check("补完有尾块", T.CONTRACT_MARK in t)
        check("尾块在 USER 段之后", t.find(T.CONTRACT_MARK) > t.find(T.ANCHOR_USER))
        check("尾块只出现一次", t.count(T.CONTRACT_MARK) == 1, str(t.count(T.CONTRACT_MARK)))
        check("记录里的版本号已对齐", (T.status()["record"] or {}).get("appVersion") == "5.9.3",
              str(T.status()["record"]))
        check("status 不再 stale", T.status()["stale"] is False)
    finally:
        sb.close()

    print("\n[B] 已是最新：一个字节都不许动")
    sb = Sandbox()
    try:
        sb.seed()
        heal_mod.heal()                      # 先补好
        before = sb.tpl.read_bytes()
        code, msg = heal_mod.heal()
        check("退出码 = 0", code == 0, f"{code} {msg}")
        check("文件逐字节未变", sb.tpl.read_bytes() == before)
    finally:
        sb.close()

    print("\n[C] 半残：插槽在、尾块被冲掉")
    sb = Sandbox()
    try:
        sb.seed()
        heal_mod.heal()
        t = sb.text()
        i, j = t.find(T.CONTRACT_MARK), t.find(T.ANCHOR_END)
        sb.seed((t[:i] + t[j:]).encode("utf-8"))
        check("样本还有插槽、没有尾块",
              bool(T.SOUL_SLOT_RE.search(sb.text())) and T.CONTRACT_MARK not in sb.text())
        code, msg = heal_mod.heal()
        check("退出码 = 1（补回）", code == 1, f"{code} {msg}")
        check("尾块回来了", T.CONTRACT_MARK in sb.text())
        check("插槽没被重复插入", sb.text().count("{{ SoulContent") == 1,
              str(sb.text().count("{{ SoulContent")))
    finally:
        sb.close()

    print("\n[D] 结构变形：锚点没了 → 停手")
    sb = Sandbox()
    try:
        broken = b"<identity_context>\r\nnothing here\r\n"
        sb.seed(broken)
        code, msg = heal_mod.heal()
        check("退出码 = 2（需要人工）", code == 2, f"{code} {msg}")
        check("文件未被改动", sb.tpl.read_bytes() == broken)
    finally:
        sb.close()

    print("\n[E] 版本号漂移：文件是好的")
    sb = Sandbox()
    try:
        sb.seed()
        heal_mod.heal()
        before = sb.tpl.read_bytes()
        sb.set_version("9.9.9")              # 宿主升级了，但模板没被覆盖
        check("样本确实 stale", T.status()["stale"] is True)
        code, msg = heal_mod.heal()
        check("退出码 = 0（只刷记录）", code == 0, f"{code} {msg}")
        check("模板内容未变", sb.tpl.read_bytes() == before)
        check("记录版本已刷新", (T.status()["record"] or {}).get("appVersion") == "9.9.9",
              str(T.status()["record"]))
        check("不再是 stale", T.status()["stale"] is False)
    finally:
        sb.close()

    print("\n[F] 锁：并发时第二个实例退 3")
    sb = Sandbox()
    try:
        sb.seed()
        lock = T.data_root() / "heal.lock"
        lock.write_text("1", encoding="utf-8")
        got, why = heal_mod._lock()
        check("锁被占 → 拿不到", got is None, str(got))
        check("原因记为 locked", why == "locked", why)
        lock.unlink()
        got2, why2 = heal_mod._lock()
        check("锁释放后能拿到", got2 is not None, why2)
        if got2 is not None:
            got2.unlink()
    finally:
        sb.close()

    print("\n[G] 开机自启：只写文件，不碰注册表/计划任务")
    sb = Sandbox()
    try:
        sb.seed()
        start_dir = sb.root / "Startup"
        start_dir.mkdir()
        saved = heal_mod.startup_dir
        heal_mod.startup_dir = lambda: start_dir      # type: ignore[assignment]
        try:
            check("未装时 installed=False", heal_mod.startup_status()["installed"] is False)
            r = heal_mod.install_startup()
            check("安装返回 ok", r.get("ok") is True, str(r))
            cmd = start_dir / heal_mod.STARTUP_CMD
            check("cmd 文件已生成", cmd.is_file())
            raw = cmd.read_bytes()
            check("cmd 是纯 ASCII（cmd.exe 按 ANSI 读，中文会乱码）",
                  all(b < 128 for b in raw), str(raw[:80]))
            body = raw.decode("ascii")
            check("cmd 带 --heal --quiet", "--heal --quiet" in body)
            check("cmd 指向当前解释器", sys.executable.split("\\")[-1] in body, body[:160])
            check("装完 installed=True", heal_mod.startup_status()["installed"] is True)
            r2 = heal_mod.install_startup()
            check("重复安装不报错", r2.get("ok") is True, str(r2))
            r3 = heal_mod.remove_startup()
            check("卸载返回 removed=True", r3.get("removed") is True, str(r3))
            check("卸载后文件没了", not cmd.exists())
            r4 = heal_mod.remove_startup()
            check("重复卸载不报错", r4.get("ok") is True and r4.get("removed") is False, str(r4))
        finally:
            heal_mod.startup_dir = saved                # type: ignore[assignment]
    finally:
        sb.close()

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
