"""在线升级：检查 → 下载 → 校验 → 暂存 → 下次启动时应用。

为什么采用「延迟应用」而不是「下载完立刻替换自己」：

  Windows 上正在运行的进程会锁住自己的文件，边跑边替换必然失败；
  而先退出再让外部脚本替换，又得杀进程、还得处理脚本自身的编码与路径问题。

  最简单可靠的路子是：**把新版解压到暂存区，下次启动时在 import core 之前覆盖**。
  那一刻 Python 只加载了几个标准库模块，磁盘上的 .py 还是旧内容，覆盖是安全的；
  覆盖完成后再继续 import，读到的就是新代码。

安全约束（每条都不是可选项）：
  · 只接受 https 的更新源（本地 fil./ 仅用于自测）
  · 下载后必须校验 sha256，对不上就丢弃
  · 包体积上限，避免被灌一个大文件
  · 解压时拦截路径穿越（Zip Slip）：成员路径必须落在暂存目录内
"""
from __future__ import annotations

import hashlib
import json
import shutil
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .paths import VERSION, data_root

# 默认更新源。指向分享页本站（2026-11-07 回迁，腾讯云云通道已废弃，不再有 /persona/ 路径前缀）。
#
# 必须带 ticket：这个地址上的静态文件不放行匿名请求，少了它就是 HTTP 403。
# 注意这份 ticket 在这里是**必需**的：下面 check_update 是拿 DEFAULT_SOURCE 原样去
# 取 version.json 的，_with_ticket 只负责把码继续传给下载地址（见其注释），
# 不会替来源地址补码。所以来源这一处必须自己带；version.json 里的 url 已带码，不会被重复追加。
DEFAULT_SOURCE = (
    "https://updates.example.com"
    ""
)

MAX_PACKAGE_BYTES = 50 * 1024 * 1024     # 50 MB
CHECK_TIMEOUT = 8.0
DOWNLOAD_TIMEOUT = 120.0


class UpdateError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class UpdateInfo:
    current: str = VERSION
    latest: str = ""
    has_update: bool = False
    url: str = ""
    sha256: str = ""
    size: int = 0
    published: str = ""
    notes: list[str] = field(default_factory=list)
    source: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "current": self.current,
            "latest": self.latest,
            "has_update": self.has_update,
            "url": self.url,
            "sha256": self.sha256,
            "size": self.size,
            "published": self.published,
            "notes": self.notes,
            "source": self.source,
            "error": self.error,
        }


# ------------------------------------------------------------------ 版本比较
def _vt(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for seg in str(v).replace("-", ".").split("."):
        digits = "".join(ch for ch in seg if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = _vt(latest), _vt(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


# ------------------------------------------------------------------ 更新源校验
def _validate_source(source: str) -> str:
    src = (source or DEFAULT_SOURCE).strip()
    parsed = urllib.parse.urlparse(src)
    if parsed.scheme in ("https", "file"):
        return src
    if parsed.scheme == "http":
        # 明文 HTTP 可以被中间人整包替换，而升级包会被完整写进程序目录，
        # 等于把任意代码执行权交出去，所以不接受。
        raise UpdateError("INSECURE_SOURCE", "更新源必须是 https（当前是 http，容易被中间人替换）")
    raise UpdateError("BAD_SOURCE", f"不认识的更新源协议：{parsed.scheme or '（空）'}")


def _with_ticket(src: str, target: str) -> str:
    """把更新源里的 ticket 继承到下载地址上。

    分享站点的静态文件需要 ticket 才放行，不带就是 403。
    从源里继承而不是把 ticket 写死在 version.json 的 url 里，有两个好处：
      1) 换源只改一处
      2) torrent 站/普通静态托管这类不需要 ticket 的源，天然不受影响
    只在同 host 时传递——跨站带上别人的 ticket 没有意义。
    """
    try:
        s = urllib.parse.urlparse(src)
        t = urllib.parse.urlparse(target)
    except ValueError:
        return target
    if not s.netloc or s.netloc.lower() != t.netloc.lower():
        return target

    code = (urllib.parse.parse_qs(s.query).get("ticket") or [None])[0]
    if not code:
        return target
    if "ticket" in urllib.parse.parse_qs(t.query):
        return target
    sep = "&" if t.query else "?"
    return target + f"{sep}ticket={urllib.parse.quote(code)}"


def _open(src: str, timeout: float):
    req = urllib.request.Request(src, headers={"User-Agent": f"persona-hub/{VERSION}"})
    ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


# ------------------------------------------------------------------ 检查
def check_update(source: str | None = None) -> UpdateInfo:
    info = UpdateInfo(source=source or DEFAULT_SOURCE)
    try:
        info.source = _validate_source(info.source)
    except UpdateError as exc:
        info.error = exc.message
        return info

    try:
        with _open(info.source, CHECK_TIMEOUT) as resp:
            raw = resp.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        info.error = f"更新源返回 HTTP {exc.code}"
        return info
    except urllib.error.URLError as exc:
        info.error = f"连不上更新源：{exc.reason}"
        return info
    except Exception as exc:  # noqa: BLE001
        info.error = f"检查失败：{type(exc).__name__}: {exc}"
        return info

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        info.error = "更新源返回的不是合法 JSON"
        return info

    if not isinstance(data, dict):
        info.error = "更新源格式不对（应为 JSON 对象）"
        return info

    info.latest = str(data.get("latest") or "").strip()
    if not info.latest:
        info.error = "更新源缺少 latest 字段"
        return info

    info.url = str(data.get("url") or "").strip()
    info.sha256 = str(data.get("sha256") or "").strip().lower()
    info.published = str(data.get("published") or "")
    notes = data.get("notes") or []
    info.notes = [str(x) for x in notes] if isinstance(notes, list) else [str(notes)]
    try:
        info.size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        info.size = 0

    info.has_update = is_newer(info.latest, VERSION)

    if info.has_update and not info.url:
        info.error = "有新版本但更新源没给下载地址"
    if info.has_update and not info.sha256:
        info.error = "有新版本但缺少 sha256，无法校验，已拒绝"
    return info


# ------------------------------------------------------------------ 目录
def updates_dir() -> Path:
    p = data_root() / "updates"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pending_file() -> Path:
    return updates_dir() / "pending.json"


def pending() -> dict | None:
    p = _pending_file()
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


# ------------------------------------------------------------------ 下载 + 暂存
def download_and_stage(info: UpdateInfo) -> dict:
    if not info.has_update:
        raise UpdateError("NO_UPDATE", "当前已是最新版本")
    if not info.url:
        raise UpdateError("NO_URL", "更新源没有提供下载地址")
    if not info.sha256:
        raise UpdateError("NO_HASH", "更新源没有提供 sha256，拒绝下载未经校验的包")

    _validate_source(info.url)

    # 下载地址补上 ticket（分享站点的静态文件匿名访问会 403）
    dl_url = _with_ticket(info.source or DEFAULT_SOURCE, info.url)

    tmp = updates_dir() / "package.zip.part"
    dst = updates_dir() / "package.zip"

    h = hashlib.sha256()
    total = 0
    try:
        with _open(dl_url, DOWNLOAD_TIMEOUT) as resp, open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PACKAGE_BYTES:
                    raise UpdateError("TOO_LARGE", f"更新包超过 {MAX_PACKAGE_BYTES // 1024 // 1024} MB，已中止")
                h.update(chunk)
                fh.write(chunk)
    except UpdateError:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        tmp.unlink(missing_ok=True)
        raise UpdateError("DOWNLOAD_FAILED", f"下载失败：{type(exc).__name__}: {exc}") from exc

    digest = h.hexdigest()
    if digest != info.sha256:
        tmp.unlink(missing_ok=True)
        raise UpdateError("HASH_MISMATCH", f"校验失败：期望 {info.sha256[:12]}…，实际 {digest[:12]}…（包已丢弃）")

    tmp.replace(dst)

    # 解压到暂存目录（每次都重建，避免上一次的残留混进来）
    staged = updates_dir() / "staged"
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(dst) as zf:
            base = staged.resolve()
            for member in zf.infolist():
                if member.is_dir():
                    continue
                name = member.filename.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    raise UpdateError("BAD_ARCHIVE", f"更新包含非法路径：{member.filename}")
                target = (base / name).resolve()
                # Zip Slip 拦截：解出来的文件必须还在暂存目录里
                if base not in target.parents and target != base:
                    raise UpdateError("BAD_ARCHIVE", f"更新包含越界路径：{member.filename}")
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    except UpdateError:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    except zipfile.BadZipFile as exc:
        shutil.rmtree(staged, ignore_errors=True)
        raise UpdateError("BAD_ARCHIVE", f"更新包不是合法的 zip：{exc}") from exc

    files = [p for p in staged.rglob("*") if p.is_file()]
    if not files:
        shutil.rmtree(staged, ignore_errors=True)
        raise UpdateError("EMPTY_ARCHIVE", "更新包里没有文件")

    # 打包成 exe 后没有 .py 可以被覆盖，更新实际上是换掉 exe 本身。
    # 而那一步必须在程序退出后做，所以这里把 exe 单独拎出来放到 updates/ 根下，
    # 告诉用户手动替换——不伪装成「已自动完成」。
    frozen = bool(getattr(__import__("sys"), "frozen", False))
    exe_path = ""
    if frozen:
        cands = [p for p in files if p.suffix.lower() == ".exe"]
        if cands:
            biggest = max(cands, key=lambda p: p.stat().st_size)
            target = updates_dir() / biggest.name
            shutil.copy2(biggest, target)
            exe_path = str(target)

    _pending_file().write_text(
        json.dumps(
            {
                "version": info.latest,
                "sha256": info.sha256,
                "staged": str(staged),
                "staged_at": __import__("time").time(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
        newline="\n",
    )

    return {
        "version": info.latest,
        "staged": str(staged),
        "files": len(files),
        "bytes": dst.stat().st_size,
        "frozen": frozen,
        "exe_path": exe_path,
        "note": (
            f"新版 exe 已存到 {exe_path}，关闭本程序后用新文件替换旧的 exe 即可。"
            if frozen and exe_path
            else ("已就绪。关闭程序后重新打开，新版本会自动生效。" if not frozen
                  else "已就绪，但包里没有 exe，无法在 exe 模式下自动替换。")
        ),
    }


# ------------------------------------------------------------------ 启动时应用
def apply_pending(root: Path | None = None) -> str | None:
    """在 import core 之前调用：把暂存内容覆盖到程序目录。

    返回一句话说明（供启动时打印），没有待应用的更新则返回 None。
    任何异常都被吞掉——更新失败不该让程序起不来，旧版本照常能用。
    """
    # exe 模式下磁盘上没有可以被覆盖的 .py，套用源码版的逻辑只是自杀。
    # 新版 exe 已由 download_and_stage 取出来，交由用户手工替换。
    if getattr(__import__("sys"), "frozen", False):
        return None

    try:
        p = pending()
        if not p:
            return None

        base = (root or Path(__file__).resolve().parent.parent).resolve()
        staged = Path(p.get("staged") or "")
        if not staged.is_dir():
            _pending_file().unlink(missing_ok=True)
            return None

        staged_res = staged.resolve()
        copied = 0
        for src in staged_res.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(staged_res)
            if ".." in rel.parts:
                continue
            dst = base / rel
            # 只覆盖程序自身的代码与资源，不碰 data/ 与用户的东西
            if rel.parts and rel.parts[0] == "data":
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1

        _pending_file().unlink(missing_ok=True)
        shutil.rmtree(staged_res, ignore_errors=True)
        pkg = updates_dir() / "package.zip"
        pkg.unlink(missing_ok=True)

        return f"已应用版本 {p.get('version', '?')}（覆盖 {copied} 个文件）"
    except Exception as exc:  # noqa: BLE001
        return f"更新应用失败（不影响使用）：{type(exc).__name__}: {exc}"
