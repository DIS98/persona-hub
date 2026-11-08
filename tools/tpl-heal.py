"""开发机上的薄包装。真正的逻辑在 `core/heal.py`。

为什么只留包装
==============
逻辑放内核，是为了让它跟着 exe 走 —— 分发出去以后对方机器上可能没有 Python，
外挂脚本就废了。这个文件只服务两种人：在本机调试的我，和想单跑一次的开发者。

    python tools/tpl-heal.py                    # 跑一次，打印结果
    python tools/tpl-heal.py --status           # 只看状态
    python tools/tpl-heal.py --install-startup  # 装开机自启
    python tools/tpl-heal.py --remove-startup   # 卸开机自启

退出码：0 无需处理 ｜ 1 本次补回 ｜ 2 需要人工 ｜ 3 被锁
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import heal as H  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--quiet", action="store_true", help="无输出，只在非 0 退出码时写日志")
    ap.add_argument("--status", action="store_true", help="只看状态，不动文件")
    ap.add_argument("--install-startup", action="store_true", help="装开机自启")
    ap.add_argument("--remove-startup", action="store_true", help="卸开机自启")
    args = ap.parse_args()

    if args.status:
        st = H.status()
        exp = st["expert"]
        print(f"state={exp['state']} soul={exp['has_soul_slot']} "
              f"contract={exp['has_contract']} stale={st['stale']}")
        print(f"startup={'on' if st['startup']['installed'] else 'off'}  {st['startup']['path']}")
        for w in st["warnings"]:
            print("warn:", w)
        return H.EXIT_OK

    if args.install_startup:
        r = H.install_startup()
        print(r)
        return H.EXIT_OK if r.get("ok") else H.EXIT_MANUAL

    if args.remove_startup:
        r = H.remove_startup()
        print(r)
        return H.EXIT_OK if r.get("ok") else H.EXIT_MANUAL

    code = H.run_quiet()
    if not args.quiet:
        print(f"[{code}] 见 {H.log_path()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
