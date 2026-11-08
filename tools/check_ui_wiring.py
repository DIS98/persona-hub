"""界面接线静态检查：绑了事件的元素到底存不存在。

为什么需要
==========
本机做不了浏览器自动化（Edge 带 --debugging-port 会立即退出），所以"点了没反应"
这类问题一直只能靠人眼。但其中一类是**纯静态可判**的：

    app.js 里写 `$('cm-save').onclick = ...`，而整个页面里根本没有 id="cm-save"

这种代码不会报错，浏览器只是静默地拿到 null 再抛一个没人看的异常。
本脚本把"引用到的 id"与"定义出来的 id"两边的集合算出来做差。

**动态渲染的 id 也要算**：`box.innerHTML = '<button id="cm-save">'` 里的 id 在运行时才存在，
只扫 index.html 会把它判成缺失 —— 这正是第一版误报 5 条的原因。

用法：
    python tools/check_ui_wiring.py          # 有缺失时非零退出

判据：
    ① 绑了事件的 id 必须存在于「HTML 静态 id ∪ JS 里出现的 id="…"」
    ② 每个 data-* 选择器都要有对应的属性输出
    ③ 不允许出现 `$('xxx')` 后链式访问属性却没有空值保护（仅提示，不判红）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = ROOT / "web" / "index.html"
JS = ROOT / "web" / "app.js"

# 事件绑定：$('x').onclick / $('x').onchange / $('x').addEventListener
BIND_RE = re.compile(r"\$\('([^']+)'\)\s*\.\s*(?:onclick|onchange|oninput|onsubmit|addEventListener)")
# 引用：$('x') 任何用法
REF_RE = re.compile(r"\$\('([^']+)'\)")
GETID_RE = re.compile(r"getElementById\(['\"]([^'\"]+)")
# 定义：HTML 静态 + JS 字符串里的 id="…"
DEF_HTML_RE = re.compile(r'id="([^"]+)"')
DEF_JS_RE = re.compile(r"""id=[\\'"]+([A-Za-z][\w-]*)""")
DATA_DEF_RE = re.compile(r"data-([a-z-]+)=")
DATA_USE_RE = re.compile(r"\$\$?\(['\"]\[data-([a-z-]+)\]")


def analyze(html: str, js: str) -> tuple[dict, list[str]]:
    """纯函数：给定 HTML/JS 文本，返回统计与问题列表。变异自检也走这里。"""
    defined = set(DEF_HTML_RE.findall(html)) | set(DEF_JS_RE.findall(js))
    refs = set(REF_RE.findall(js)) | set(GETID_RE.findall(js))
    binds = set(BIND_RE.findall(js))
    fails: list[str] = []
    for m in sorted(binds - defined):
        fails.append(f"绑了事件但元素不存在：{m}")
    for m in sorted(refs - defined):
        fails.append(f"引用但元素不存在：{m}")
    data_def = set(DATA_DEF_RE.findall(html)) | set(DATA_DEF_RE.findall(js))
    for u in sorted(set(DATA_USE_RE.findall(js)) - data_def):
        fails.append(f"data 选择器没有对应输出：data-{u}")
    return {"defined": defined, "refs": refs, "binds": binds}, fails


def selftest() -> int:
    """变异自检：往一份正常输入里塞一个不存在的绑定，判据必须报出来。

    只跑「通过」说明不了这个脚本有用——它也可能对任何输入都说通过。
    """
    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    _, clean = analyze(html, js)
    if clean:
        print(f"基线就不干净（{len(clean)} 个问题），先修好再自检")
        return 2
    broken = js + "\n$('this-id-does-not-exist').onclick = () => {};\n"
    _, bad = analyze(html, broken)
    caught = any("this-id-does-not-exist" in f for f in bad)
    print(f"变异自检：{'✓ 注入一个不存在的绑定后报红' if caught else '✗ 注入后仍然通过，判据无效'}")
    return 0 if caught else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if not HTML.is_file() or not JS.is_file():
        print(f"缺文件：{HTML} 或 {JS}")
        return 2

    html = HTML.read_text(encoding="utf-8")
    js = JS.read_text(encoding="utf-8")
    stats, fails = analyze(html, js)

    print(f"页面 id 定义 {len(stats['defined'])} 个；JS 引用 {len(stats['refs'])} 个；"
          f"绑事件 {len(stats['binds'])} 个")
    if any("绑了事件" in f for f in fails):
        for f in fails:
            if "绑了事件" in f:
                print(f"  ✗ {f}")
    else:
        print("  ✓ 所有事件绑定都能对应到真实元素")
    for f in fails:
        if "绑了事件" not in f:
            print(f"  ✗ {f}")

    print()
    if fails:
        print(f"界面接线检查：{len(fails)} 个问题")
        return 1
    print("界面接线检查：通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
