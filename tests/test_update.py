"""在线升级的测试。

重点不是「能不能升级」，而是「不该升级的时候能不能拦住」：
  · 明文 http 必须拒
  · sha256 对不上必须丢包
  · 非 zip 必须拒
  · 含路径穿越的包必须拒
  · 应用更新时不碰 data/ 与用户数据
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="ph-upd-"))
os.environ["PERSONA_HUB_HOME"] = str(TMP / "data")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import update as U  # noqa: E402
from core.paths import VERSION  # noqa: E402

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
    return cond


def expect_error(name: str, fn, code: str | None = None) -> bool:
    try:
        fn()
    except U.UpdateError as e:
        if code and e.code != code:
            return check(name, False, f"期望 {code}，实际 {e.code}")
        return check(name, True)
    except Exception as e:  # noqa: BLE001
        return check(name, False, f"抛了非 UpdateError: {type(e).__name__}: {e}")
    return check(name, False, "没有抛异常")


def make_zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)


def file_url(p: Path) -> str:
    return "fil./" + str(p).replace("\\", "/")


def main() -> int:
    print(f"当前版本: {VERSION}\n")

    # ---------------------------------------------------------------- 版本比较
    print("[版本比较]")
    check("1.2.0 > 1.1.9", U.is_newer("1.2.0", "1.1.9"))
    check("1.2.0 == 1.2.0 不算更新", not U.is_newer("1.2.0", "1.2.0"))
    check("1.2.1 > 1.2.0", U.is_newer("1.2.1", "1.2.0"))
    check("1.10.0 > 1.9.0（按数字比不是按字符串）", U.is_newer("1.10.0", "1.9.0"))
    check("2.0 < 10.0 判断正确", U.is_newer("10.0", "2.0"))
    check("空串按 0 处理", U._vt("") == (0,))

    # ---------------------------------------------------------------- 源校验
    print("\n[更新源校验]")
    expect_error("拒绝 http 明文源", lambda: U._validate_source("http://x/version.json"), "INSECURE_SOURCE")
    expect_error("拒绝未知协议", lambda: U._validate_source("ftp://x/v.json"), "BAD_SOURCE")
    ok = True
    try:
        U._validate_source("https://x/version.json")
        U._validate_source("fil./")
    except Exception:
        ok = False
    check("接受 https 与 file", ok)

    # ---------------------------------------------------------------- 正常检查
    print("\n[检查更新]")
    src_dir = TMP / "src"
    src_dir.mkdir()

    pkg = src_dir / "pkg.zip"
    make_zip(pkg, {"hub.py": "# new hub\n", "core/paths.py": "VERSION='9.9.9'\n"})
    sha = hashlib.sha256(pkg.read_bytes()).hexdigest()

    vj = src_dir / "version.json"
    vj.write_text(json.dumps({
        "latest": VERSION, "url": file_url(pkg), "sha256": sha,
        "size": pkg.stat().st_size, "notes": ["测试用"],
    }), encoding="utf-8")

    info = U.check_update(file_url(vj))
    check("同版本判定为已是最新", not info.has_update)
    check("current 正确", info.current == VERSION)
    check("latest 解析正确", info.latest == VERSION)
    check("无错误", info.error == "", info.error)

    # 更高版本
    vj2 = src_dir / "version2.json"
    vj2.write_text(json.dumps({
        "latest": "99.0.0", "url": file_url(pkg), "sha256": sha,
        "notes": ["修了些问题", "加了些东西"],
    }), encoding="utf-8")
    info2 = U.check_update(file_url(vj2))
    check("更高版本判定为有更新", info2.has_update)
    check("更新说明解析为列表", info2.notes == ["修了些问题", "加了些东西"], str(info2.notes))

    # 缺 sha256 → 拒绝
    vj3 = src_dir / "version3.json"
    vj3.write_text(json.dumps({"latest": "99.0.0", "url": file_url(pkg)}), encoding="utf-8")
    info3 = U.check_update(file_url(vj3))
    check("有更新但缺 sha256 时报错", bool(info3.error) and not info3.as_dict().get("has_update", True) or bool(info3.error), info3.error)

    # 坏 JSON
    bad = src_dir / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    info4 = U.check_update(file_url(bad))
    check("坏 JSON 有可读错误", "JSON" in info4.error, info4.error)

    # 不存在的源
    info5 = U.check_update(file_url(src_dir / "nope.json"))
    check("源不存在时返回错误而非崩溃", bool(info5.error), info5.error)

    # ---------------------------------------------------------------- 下载与校验
    print("\n[下载与校验]")
    ok_info = U.UpdateInfo(current=VERSION, latest="99.0.0", has_update=True,
                           url=file_url(pkg), sha256=sha, notes=[])
    res = U.download_and_stage(ok_info)
    check("下载并暂存成功", res["files"] == 2, str(res))
    check("暂存目录已生成", Path(res["staged"]).is_dir())
    check("pending.json 已写入", U.pending() is not None)
    check("pending 版本正确", (U.pending() or {}).get("version") == "99.0.0")

    # 哈希不匹配 → 丢包
    bad_info = U.UpdateInfo(current=VERSION, latest="99.0.1", has_update=True,
                            url=file_url(pkg), sha256="0" * 64, notes=[])
    before = len(list(U.updates_dir().glob("package.zip*")))
    expect_error("哈希不匹配被拒", lambda: U.download_and_stage(bad_info), "HASH_MISMATCH")
    after = len(list(U.updates_dir().glob("package.zip*")))
    check("哈希不匹配后不留包", after <= before, f"{before} -> {after}")

    # 没有 sha256 的包直接拒
    expect_error("无 sha256 拒绝下载",
                 lambda: U.download_and_stage(U.UpdateInfo(current=VERSION, latest="9.9.9",
                                                           has_update=True, url=file_url(pkg), sha256="")),
                 "NO_HASH")

    # 无更新时不能下载
    expect_error("无更新时拒绝下载",
                 lambda: U.download_and_stage(U.UpdateInfo(current=VERSION, latest=VERSION,
                                                           has_update=False, url=file_url(pkg), sha256=sha)),
                 "NO_UPDATE")

    # ---------------------------------------------------------------- 恶意包
    print("\n[恶意包拦截]")

    # 非 zip
    notzip = src_dir / "notzip.bin"
    notzip.write_bytes(b"this is not a zip at all")
    nz_sha = hashlib.sha256(notzip.read_bytes()).hexdigest()
    expect_error("非 zip 包被拒",
                 lambda: U.download_and_stage(U.UpdateInfo(current=VERSION, latest="99.1.0",
                                                           has_update=True, url=file_url(notzip), sha256=nz_sha)),
                 "BAD_ARCHIVE")

    # 路径穿越（Zip Slip）
    evil = src_dir / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")
        zf.writestr("hub.py", "# ok\n")
    evil_sha = hashlib.sha256(evil.read_bytes()).hexdigest()
    expect_error("路径穿越包被拒",
                 lambda: U.download_and_stage(U.UpdateInfo(current=VERSION, latest="99.2.0",
                                                           has_update=True, url=file_url(evil), sha256=evil_sha)),
                 "BAD_ARCHIVE")

    # 空包
    empty = src_dir / "empty.zip"
    make_zip(empty, {})
    e_sha = hashlib.sha256(empty.read_bytes()).hexdigest()
    expect_error("空包被拒",
                 lambda: U.download_and_stage(U.UpdateInfo(current=VERSION, latest="99.3.0",
                                                           has_update=True, url=file_url(empty), sha256=e_sha)),
                 "EMPTY_ARCHIVE")

    # ---------------------------------------------------------------- 应用
    print("\n[应用更新]")

    inst = TMP / "install"
    (inst / "data").mkdir(parents=True)
    (inst / "hub.py").write_text("# old hub\n", encoding="utf-8")
    (inst / "data" / "keep.txt").write_text("用户数据", encoding="utf-8")

    staged = Path(res["staged"])
    # 模拟一个包：含程序文件 + 一个试图覆盖 data/ 的文件
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    (staged / "hub.py").write_text("# new hub\n", encoding="utf-8")
    (staged / "core").mkdir()
    (staged / "core" / "server.py").write_text("# new server\n", encoding="utf-8")
    (staged / "data").mkdir()
    (staged / "data" / "keep.txt").write_text("应该被忽略", encoding="utf-8")
    (staged / "data" / "new.txt").write_text("也不该写进去", encoding="utf-8")

    U._pending_file().write_text(json.dumps({"version": "99.0.0", "staged": str(staged)}), encoding="utf-8")
    msg = U.apply_pending(inst)
    check("应用返回说明", bool(msg) and "99.0.0" in (msg or ""), str(msg))
    check("hub.py 被更新", (inst / "hub.py").read_text(encoding="utf-8") == "# new hub\n")
    check("新增文件已落地", (inst / "core" / "server.py").is_file())
    check("data/ 未被覆盖", (inst / "data" / "keep.txt").read_text(encoding="utf-8") == "用户数据")
    check("data/ 未新增文件", not (inst / "data" / "new.txt").exists())
    check("pending 已清除", U.pending() is None)
    check("暂存目录已清理", not staged.exists())

    # 再次调用应当无操作
    check("无待应用更新时返回 None", U.apply_pending(inst) is None)

    # 坏 pending 不应让启动失败
    U._pending_file().write_text("{broken", encoding="utf-8")
    check("坏 pending 被安全忽略", U.apply_pending(inst) is None)

    print("\n" + "=" * 54)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILED:
        print("失败清单：")
        for n in FAILED:
            print("  ·", n)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
