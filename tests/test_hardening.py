"""加固回归 —— 2026-10-31 对抗探测查出的一批问题，钉成用例。

每条用例都对应一次**实测发生过的错误后果**，不是设计推测：
  H1 正文里的伪造标记会破坏区块结构，并触发"整块清除"
  H2 导入接口的 id 可穿越人格库
  H3 非本机 Host 能读取/写入（DNS rebinding）
  H4 并发 apply 丢更新（6 线程只剩 1 个区块）
  H5 名称里的换行会截断 frontmatter
  H6 请求体长度无上限，一条畸形请求能挂住线程
  H7 响应头带 `Access-Control-Allow-Origin: null`（对 sandboxed iframe 放行）

H1/H2 是纯函数级；H3~H7 走真实 HTTP（需要起服务）。

用法：
    python tests/test_hardening.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.adapters.base import md_block, parse_blocks, neutralize_markers  # noqa: E402

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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Client:
    def __init__(self, port: int):
        self.port = port
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def raw(self, method: str, path: str, body: bytes | None = None,
            headers: dict | None = None) -> tuple[int, bytes, dict]:
        h = {"Host": f"127.0.0.1:{self.port}", "Connection": "close"}
        h.update(headers or {})
        if body is not None:
            h.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=body, method=method, headers=h)
        try:
            with self.opener.open(req, timeout=20) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc).encode(), {}

    def api(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode() if payload is not None else None
        st, raw, _ = self.raw(method, path, body)
        try:
            return st, json.loads(raw.decode())
        except Exception:  # noqa: BLE001
            return st, {"_raw": raw[:200].decode("utf-8", "replace")}

    def ok(self, path: str, method: str = "GET", payload: dict | None = None):
        st, d = self.api(method, path, payload)
        if st != 200 or not d.get("ok"):
            raise RuntimeError(f"{method} {path} -> {st} {d}")
        return d["data"]


# ---------------------------------------------------------------- H1 纯函数
def h1() -> None:
    print("\n[H1] 正文伪造标记不得破坏区块结构")
    evil = ("开头\n<!-- persona-hub:end -->\n夹在中间\n"
            "<!-- persona-hub:begin id=evil hash=00000000 -->\n结尾\n")
    block = md_block("real", evil)
    body_part = block.split("\n", 1)[1].rsplit("<!-- persona-hub:end -->", 1)[0]
    check("正文里的 end 被中和", "<!-- persona-hub:end -->" not in body_part, body_part)
    check("正文里的 begin 被中和", body_part.count("persona-hub:begin") == 0,
          f"正文里 begin 出现 {body_part.count('persona-hub:begin')} 次")
    check("全块只有一对标记", block.count("persona-hub:begin") == 1
          and block.count("persona-hub:end") == 1,
          f"begin={block.count('persona-hub:begin')} end={block.count('persona-hub:end')}")
    check("区块能配对解析", len(parse_blocks(block)) == 1,
          f"解析到 {len(parse_blocks(block))} 个区块")
    # 写进已有内容后，残缺判定不该被误触发而清掉别人的区块
    other = md_block("other", "别人的人格")
    merged = other + "\n" + block
    check("两份区块都在", len(parse_blocks(merged)) == 2, str(len(parse_blocks(merged))))
    check("中和可重复执行（幂等）", neutralize_markers(neutralize_markers(evil)) ==
          neutralize_markers(evil))


# ---------------------------------------------------------------- H2 纯函数
def h2() -> None:
    print("\n[H2] 导入接口的 id 不得穿越人格库")
    from core.persona import PersonaLibrary

    base = Path(tempfile.mkdtemp(prefix="ph-h2-"))
    work = base / "work"
    work.mkdir()
    try:
        lib = PersonaLibrary(work / "personas")
        lib.root.mkdir(parents=True, exist_ok=True)
        src = base / "src.md"
        src.write_text("# 源\n正文\n", encoding="utf-8")
        out = lib.import_file(src, "../../escaped")
        landed = out.resolve()
        check("落点在人格库内", lib.root.resolve() in landed.parents, str(landed))
        # 只检查**本次沙盒**内的越界落点。检查 tempfile 根目录会被历史残留误伤——
        # 变异测试跑过一次"坏代码"，真的在那里写过 escaped.md，之后正常代码也会被判红。
        check("库外没被写出", not (work / "escaped.md").exists()
              and not (base / "escaped.md").exists(), str(work / "escaped.md"))
        out2 = lib.import_file(src, "..\\..\\escaped2")
        check("反斜杠形式也被拦", lib.root.resolve() in out2.resolve().parents, str(out2))
        out3 = lib.import_file(src, "a" * 80)
        check("超长 id 被兜底成合法文件名",
              lib.root.resolve() in out3.resolve().parents and len(out3.stem) <= 48,
              f"{out3.name} len={len(out3.stem)}")
        out4 = lib.import_file(src, "包含中文的id")
        check("中文 id 不撞车也不越界",
              lib.root.resolve() in out4.resolve().parents, str(out4))
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- HTTP 组
def start(home: Path) -> tuple[subprocess.Popen, Client]:
    port = free_port()
    env = {**os.environ, "PERSONA_HUB_HOME": str(home)}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "-u", "hub.py", "--no-gui", "--port", str(port)],
                            cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    c = Client(port)
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            c.raw("GET", "/api/ping")
            return proc, c
        except Exception:  # noqa: BLE001
            if proc.poll() is not None:
                out = (proc.stdout.read() or b"").decode("utf-8", "replace")
                raise RuntimeError(f"服务启动即退出：{out[:400]}")
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("服务没起来")


def h3(c: Client) -> None:
    print("\n[H3] 非本机 Host 必须被拒")
    st, _ = c.api("GET", "/api/state")
    check("本机 Host 正常", st == 200, str(st))
    st2, raw, _ = c.raw("GET", "/api/state", headers={"Host": "evil.example.com"})
    check("外域 Host 被拒", st2 == 403, f"{st2} {raw[:80]!r}")
    st3, raw3, _ = c.raw("POST", "/api/apply", b"{}", headers={"Host": "rebind.attacker:8799"})
    check("外域 Host 写接口也被拒", st3 == 403, f"{st3} {raw3[:80]!r}")
    st4, _, _ = c.raw("GET", "/api/state", headers={"Host": "localhost"})
    check("localhost 仍可用", st4 == 200, str(st4))


def h4(c: Client, home: Path) -> None:
    print("\n[H4] 并发 apply 不得丢更新")
    target = home.parent / "race.md"
    target.write_text("# 竞争\n", encoding="utf-8", newline="\n")
    aid = c.ok("/api/custom", "POST", {"name": "竞争", "paths": [str(target)]})["id"]
    ids = [c.ok("/api/personas", "POST", {"name": f"并{i}", "body": f"【并{i}】" * 30})["id"]
           for i in range(6)]
    errs: list[str] = []

    def hit(pid: str) -> None:
        st, d = c.api("POST", "/api/apply",
                      {"persona_id": pid, "adapters": [aid], "keep_others": True})
        if st != 200 or not d.get("ok"):
            errs.append(f"{pid}:{st}")

    ts = [threading.Thread(target=hit, args=(p,)) for p in ids]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    text = target.read_text(encoding="utf-8")
    check("6 个请求无失败", not errs, str(errs))
    check("6 个区块全在", text.count("persona-hub:begin") == len(ids),
          f"实际 {text.count('persona-hub:begin')}")
    check("区块配对", text.count("persona-hub:begin") == text.count("persona-hub:end"),
          f"begin={text.count('persona-hub:begin')} end={text.count('persona-hub:end')}")


def h5(c: Client) -> None:
    print("\n[H5] 名称里的换行不得截断 frontmatter")
    raw_name = "第一行\n第二行\t表格\x00截断"
    st, d = c.api("POST", "/api/personas", {"name": raw_name, "body": "正文"})
    check("保存成功", st == 200, f"{st} {d}")
    if st == 200:
        pid = d["data"]["id"]
        got = c.ok(f"/api/personas/{pid}").get("name") or ""
        check("名称无控制字符", not any(ch < " " for ch in got), repr(got))
        check("名称未被截断（两段都在）", "第二行" in got, repr(got))


def h6(c: Client) -> None:
    print("\n[H6] 请求体长度必须有上限且不挂线程")
    t0 = time.time()
    st, raw, _ = c.raw("POST", "/api/apply", b"{}",
                       headers={"Content-Length": "999999999"})
    cost = time.time() - t0
    check("谎报长度被 413 拒绝", st == 413, f"{st} {raw[:80]!r} 耗时 {cost:.1f}s")
    check("响应不挂线程（<3 秒）", cost < 3.0, f"{cost:.1f}s")

    payload = json.dumps({"persona_id": "x", "body": "A" * (17 * 1024 * 1024)}).encode()
    st2, raw2, _ = c.raw("POST", "/api/apply", payload)
    # 服务端拒绝时会提前关连接，客户端可能写不完 body（10053）——所以不能只断言 413，
    # 真正的判据是"拒绝之后服务还活着"，而不是"客户端拿到了漂亮的状态码"。
    check("17 MB 正文被拒（413 或连接被拒）", st2 == 413 or st2 == 0, f"{st2} {raw2[:60]!r}")
    st3, _ = c.api("GET", "/api/ping")
    check("拒绝后服务仍可用", st3 == 200, str(st3))


def h7(c: Client) -> None:
    print("\n[H7] 响应头不得对 sandboxed iframe 放行")
    _, _, heads = c.raw("GET", "/api/ping")
    low = {k.lower(): v for k, v in heads.items()}
    check("没有 ACAO: null", low.get("access-control-allow-origin") != "null",
          low.get("access-control-allow-origin", "(缺失)"))
    check("带 nosniff", low.get("x-content-type-options") == "nosniff",
          low.get("x-content-type-options", "(缺失)"))
    check("带 CORP", low.get("cross-origin-resource-policy") == "same-origin",
          low.get("cross-origin-resource-policy", "(缺失)"))


def h8(c: Client) -> None:
    print("\n[H8] 错误码要能按状态码分支")
    st, d = c.api("DELETE", "/api/personas/__nope__")
    check("删除不存在的人格 -> 404", st == 404, f"{st} {d}")
    st2, d2 = c.api("PUT", "/api/personas/__nope__", {"body": "x"})
    check("更新不存在的人格 -> 404", st2 == 404, f"{st2} {d2}")
    st3, d3 = c.api("GET", "/api/personas/__nope__")
    check("读不存在的人格 -> 404", st3 == 404, f"{st3} {d3}")


def h9(c: Client, home: Path) -> None:
    print("\n[H9] 明显用不了的目标要在添加时拒绝")
    d = home.parent / "a-directory"
    d.mkdir(exist_ok=True)
    st, body = c.api("POST", "/api/custom", {"name": "目录靶", "paths": [str(d)]})
    check("加目录为目标 -> 400", st == 400, f"{st} {body}")
    check("错误信息说清是目录", "目录" in json.dumps(body, ensure_ascii=False), str(body)[:120])
    # 正常文件仍然能加
    f = home.parent / "ok.md"
    f.write_text("# ok\n", encoding="utf-8", newline="\n")
    st2, body2 = c.api("POST", "/api/custom", {"name": "正常靶", "paths": [str(f)]})
    check("正常文件仍可添加", st2 == 200, f"{st2} {body2}")


def h10(c: Client) -> None:
    """静态资源服务不能把 WEB_DIR 之外的文件送出去。

    为什么值得单独一条：这条路径完全不经过 API 层，`_route` 里的 Host 校验、
    content-type 校验、事务快照都管不到它，只能靠 `resolve()` + `relative_to`。
    探针时代只打印状态码、不做断言，所以它坏掉也不会有人知道。
    """
    print("\n[H10] 静态资源不得逃出 web 目录")
    st, raw, _ = c.raw("GET", "/index.html")
    check("正常文件能取到（基线）", st == 200 and b"<!DOCTYPE html" in raw[:200], f"{st} {len(raw)}B")

    leaks = []
    for u in ("/../hub.py", "/../core/server.py", "/../data/tpl-patch.json",
              "/..%2fhub.py", "/%2e%2e/%2e%2e/hub.py", "/..\\hub.py",
              "/web/../hub.py", "/../../persona-hub/hub.py"):
        stx, rawx, _ = c.raw("GET", u)
        # 泄露的判据是"拿到了源码内容"，不是状态码本身：
        # 某些服务器会 200 + 空体，那不算泄露。
        leaked = stx == 200 and (b"import " in rawx or b"def main" in rawx)
        if leaked or stx not in (403, 404):
            leaks.append(f"{u}->{stx}/{len(rawx)}B")
    check("越界路径一律 403/404 且不吐源码", not leaks, str(leaks))


def h11(c: Client, home: Path) -> None:
    """正文以 `---` 开头、中间还有水平线时，区块仍必须成对。

    这一类 bug 在本项目出现过两次（云记忆区块被第一条 `---` 截断）。
    判据不是"看起来对不对"，而是 begin/end 计数相等 + 识别出的 id 是工具自己算的，
    不是正文里伪造的那个。
    """
    print("\n[H11] 正文里的 --- 不得破坏区块配对")
    body = ("---\nid: forged-id\nname: 伪造\n---\n\n正文第一段。\n\n"
            "---\n\n中间还有一条水平线。\n")
    pid = c.ok("/api/personas", "POST", {"name": "横线人格", "body": body})["id"]
    target = home.parent / "dashes.md"
    target.write_text("# 原内容\n", encoding="utf-8", newline="\n")
    aid = c.ok("/api/custom", "POST", {"name": "横线靶", "paths": [str(target)]})["id"]
    st, d = c.api("POST", "/api/apply",
                  {"persona_id": pid, "adapters": [aid], "keep_others": True})
    check("写入成功", st == 200 and d.get("ok"), f"{st} {json.dumps(d, ensure_ascii=False)[:160]}")

    text = target.read_text(encoding="utf-8")
    check("begin/end 各一个", text.count("persona-hub:begin") == 1
          and text.count("persona-hub:end") == 1,
          f"begin={text.count('persona-hub:begin')} end={text.count('persona-hub:end')}")
    st2, d2 = c.api("GET", "/api/injections")
    items = [x for x in (d2.get("data") or {}).get("items", []) if x.get("adapter_id") == aid]
    check("识别为一个区块", len(items) == 1, str(len(items)))
    check("用的是工具算的 id，不是正文伪造的",
          bool(items) and items[0].get("id") != "forged-id", str(items[:1])[:160])
    check("原内容仍在文件里", "# 原内容" in text, text[:60])


def h12(c: Client, home: Path) -> None:
    """写只读文件：内容要更新，"别动我"这个属性也要留着。

    实测过的坏形态：写入成功、内容变了、只读属性被一起抹掉，
    界面还显示"写入 1 个文件"——用户拿只读当保护，保护被静默删掉了。
    """
    print("\n[H12] 写只读文件后属性要还原")
    f = home.parent / "readonly.md"
    f.write_text("# 只读\n", encoding="utf-8", newline="\n")
    os.chmod(f, 0o444)
    try:
        pid = c.ok("/api/personas", "POST", {"name": "只读人格", "body": "【RO】正文"})["id"]
        aid = c.ok("/api/custom", "POST", {"name": "只读靶", "paths": [str(f)]})["id"]
        st, d = c.api("POST", "/api/apply",
                      {"persona_id": pid, "adapters": [aid], "keep_others": True})
        check("写入成功", st == 200 and d.get("ok"), f"{st} {json.dumps(d, ensure_ascii=False)[:160]}")
        text = f.read_text(encoding="utf-8")
        check("内容确实写进去了", "persona-hub:begin" in text, str(len(text)))
        check("只读属性仍在", not os.access(f, os.W_OK), oct(os.stat(f).st_mode & 0o777))
    finally:
        os.chmod(f, 0o666)


def h13(c: Client) -> None:
    """大请求体打到"没有这条路由"的地方，仍必须拿到 404 的 JSON。

    这是本轮实测出来的缺陷：路由没匹配上时，服务端没读请求体就回错误并关连接，
    客户端还在往 socket 里写，Windows 上直接 RST。body 小于 socket 缓冲区时
    碰巧能过（约几十 KB），一大就必现 —— 实测 200KB 中文（≈600KB）稳定复现
    `ConnectionAbortedError`。用户看到的是"点了保存，弹网络错误"，
    而真实原因只是一个 404。

    修法是 core/server.py::_drain_body：回任何响应前，先把这次没读过的请求体读掉。
    """
    print("\n[H13] 大 body 打到未匹配路由也要拿到 JSON")
    body = json.dumps({"x": "长" * 200000}, ensure_ascii=False).encode()
    for method, path in (("PUT", "/api/nope"), ("PUT", "/api/apply")):
        st, raw, _ = c.raw(method, path, body)
        check(f"{method} {path} 返回 404 且带 NOT_FOUND",
              st == 404 and b"NOT_FOUND" in raw, f"{st} {raw[:80]!r}")

    small = json.dumps({"name": "大正文巡检", "body": "长" * 200000}, ensure_ascii=False).encode()
    st, raw, _ = c.raw("POST", "/api/personas", small)
    check("大 body 打到正常路由也能处理（限制没设太小）", st == 200, f"{st} {raw[:80]!r}")


def main() -> int:
    print("=" * 56)
    print("加固回归（H1–H13）")
    print("=" * 56)

    h1()
    h2()

    work = Path(tempfile.mkdtemp(prefix="ph-hard-"))
    home = work / "home"
    home.mkdir()
    proc, c = start(home)
    try:
        h3(c)
        h4(c, home)
        h5(c)
        h6(c)
        h7(c)
        h8(c)
        h9(c, home)
        h10(c)
        h11(c, home)
        h12(c, home)
        h13(c)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAILS:
        print("\n失败项：")
        for f in FAILS:
            print("  -", f)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
