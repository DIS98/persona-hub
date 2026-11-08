"""变异测试：验证「USER.md 出现在预算里 + 窗口=10000」这条判据能不能抓错。

判据本身通过没有意义——要证明它**会报红**。三个变体：
  A 把窗口设回 None（= 改之前的状态）→ USER.md 应从预算里消失
  B 把窗口改成 4000（"顺手统一两个通道"的典型错法）→ limit 应变 4000
  C 正常值 → 必须通过

只读，不写任何客户端文件（budget 只读文件长度）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import server as srv  # noqa: E402
from core.adapters.workbuddy_identity import WorkBuddyUserAdapter  # noqa: E402

real_all = srv.all_adapters

PASS = FAIL = 0


def expect(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {name}")
    else:
        FAIL += 1
        print(f"  \u2717 {name}  -> {detail}")


def user_item() -> dict | None:
    app = srv.App()
    for it in app.budget()["items"]:
        if it.get("file") == "USER.md":
            return it
    return None


def main() -> int:
    original = WorkBuddyUserAdapter.read_limit

    # ---- 基线（C）
    print("\n[C] 正常值 10000")
    it = user_item()
    expect("USER.md 出现在预算里", it is not None, "items 里没有 USER.md")
    expect("窗口 == 10000", bool(it) and it["limit"] == 10000, str(it and it["limit"]))

    # ---- 变异 A：窗口设回 None
    print("\n[A] 变异：read_limit=None（改之前的状态）")
    WorkBuddyUserAdapter.read_limit = None
    it_a = user_item()
    expect("判据报红——USER.md 从预算里消失", it_a is None, f"仍然存在：{it_a}")

    # ---- 变异 B：窗口抄成 4000
    print("\n[B] 变异：read_limit=4000（把两个通道合成一个常量）")
    WorkBuddyUserAdapter.read_limit = 4000
    it_b = user_item()
    expect("判据报红——窗口变成 4000", bool(it_b) and it_b["limit"] != 10000,
           f"limit={it_b and it_b['limit']}")

    WorkBuddyUserAdapter.read_limit = original
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
