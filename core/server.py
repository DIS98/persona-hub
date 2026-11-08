"""本地 HTTP API 与静态资源服务。

设计约束（对应「接口处理更完美」）：

  - **只绑 127.0.0.1**，不监听 0.0.0.0。这类工具没有任何理由对外开端口。
  - **统一响应信封**：`{"ok": true, "data": ...}` / `{"ok": false, "error": {"code","message"}}`
  - **业务错误带稳定错误码**，前端可据此分支，不靠解析文案
  - **写操作全部两段式**：先 `preview` 拿 diff，再 `apply` 提交；apply 内部是单事务
  - 所有异常在边界处转为错误响应，不让 traceback 漏到连接上（也便于排查）
"""
from __future__ import annotations

import functools
import json
import mimetypes
import re
import socket
import threading
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .adapters import all_adapters, get_adapter, load_custom, save_custom
from .paths import (
    APP_NAME_CN,
    VERSION,
    WEB_DIR,
    data_root,
    host_processes,
    log_path,
    vault_dir,
)
from .persona import PersonaError, PersonaLibrary, Persona
from .txn import Transaction
from .vault import Vault

DEFAULT_PORT = 8799

# 哪些通道是「写进去下一轮就一定读到」的 —— 目前**没有任何一条**敢这么标。
#
# 曾经这里写过 {"USER.md"}，依据是一次观察：清掉 USER.md 里的标记后，
# 下一轮对话的 <identity_context> 内容跟着变了。**那个结论是错的**——
# 用户实测：注入人格、不重启 WorkBuddy，下一轮没有生效；而且 identity_context
# 在后面几轮里根本不出现在上下文里。一次观察被外推成了普适规则，代价是让用户白等一轮。
#
# 现在的判断：USER.md / SOUL.md 这类身份文件是**会话级**注入，宿主什么时候重读不可靠。
# 按“不确定”处理 —— 告警里给可执行的建议，不给侥幸的结论。
LIVE_CHANNELS: set[str] = set()

# 请求体上限。人格正文最大也就几百 KB，16 MB 已经宽松得离谱。
#
# 实测（2026-10-31）：把 Content-Length 谎报成 999,999,999 而只发几十字节，
# 服务端会一直 read 到客户端超时——单线程被一条畸形请求占住。加个上限即可。
MAX_BODY_BYTES = 16 * 1024 * 1024
# 页面心跳的有效期（秒）。页面每 5 秒打一次 ping?page=1，超过这个窗口没打就认为
# "没有窗口在看"。用途只有一个：再启动一次时决定"补一个窗口"还是"别开第二个"。
PAGE_ALIVE_WINDOW = 15

# 允许的 Host。只绑 127.0.0.1 挡不住 DNS rebinding：
# 恶意站点把自己的域名解析到 127.0.0.1，浏览器就认为是同源，本机 API 全暴露。
# 实测：Host: evil.example.com 请求 /api/state 返回 200 与完整状态。
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def serialized(method):
    """写操作串行化。

    实测（2026-10-31）：6 个线程同时 apply 到同一个文件，最后文件里只剩 1 个区块——
    apply 是「读全文 → 改 → 写回」，中间没有任何互斥，后写的把先写的读到的旧内容盖回去。
    ThreadingHTTPServer 本来就是多线程的，所以这是必然，不是偶发。
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._write_lock:
            return method(self, *args, **kwargs)

    return wrapper


def clean_name(raw: object) -> str:
    """压平名称里的换行与控制字符。

    实测：名称写 `换行\\n名字` 存进去，读回来只剩 `换行`——frontmatter 是逐行解析的，
    换行会把 name 截断，后面几行变成无法解析的垃圾键值。写入时压平比事后解释便宜。
    """
    s = str(raw or "")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    s = "".join(ch for ch in s if ch >= " " and ch != "\x7f")
    return s.strip()


def _audit(line: str) -> None:
    """云记忆是宿主内部状态，读写都必须留痕——出事了要能倒查是哪一次动的。"""
    try:
        with open(log_path(), "a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [cloudmem] {line}\n")
    except OSError:
        pass



class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# --------------------------------------------------------------------- 业务层
class App:
    def __init__(self) -> None:
        self.vault = Vault(vault_dir())
        self.library = PersonaLibrary(data_root() / "personas")
        # 页面在 beforeunload 时会打 /api/bye，置位后入口进程即可安心退出
        self.closing = False
        # 页面心跳时刻（只有带 page=1 的 ping 会更新它）。
        # 为什么不复用 last_request：外壳探活、测试脚本、命令行 curl 都会产生请求，
        # 拿它们当"有窗口在看"，双击第二次就永远补不出窗口。
        self.last_page_ping = 0.0
        # 写操作互斥锁（见 serialized）：apply 是读-改-写，多线程必须串行
        self._write_lock = threading.RLock()
        # 首次启动时把内置模板复制进可写目录
        self._seed_personas()

    def _seed_personas(self) -> None:
        from .paths import PERSONAS_DIR

        if not PERSONAS_DIR.is_dir():
            return
        for src in PERSONAS_DIR.glob("*.md"):
            dst = self.library.root / src.name
            if not dst.exists():
                dst.write_bytes(src.read_bytes())

    # ---------------------------------------------------------------- 预算
    # ------------------------------------------------ 生效自检 与 云记忆
    def _identity_files(self) -> list[Path]:
        """本次自检要对照的身份文件（只取真实存在、且是本工具会写的那些）。"""
        out: list[Path] = []
        for a in all_adapters():
            if not a.detect().detected:
                continue
            for t in a.detect().targets:
                if t.path.is_file() and t.path.name in {"USER.md", "SOUL.md"}:
                    out.append(t.path)
        return out

    def verify_injection(self, host: str | None = None) -> dict:
        """从宿主自己的会话记录里读回「这一轮到底读到了什么」。

        两个宿主的取证位置完全不同，各自一个模块：
          WorkBuddy —— ~/.workbuddy/projects/*/*.jsonl 的 <identity_context>
          DSH       —— ~/.dsh/sessions/**/session.v4.jsonl.zstd 的 agent-instructions

        顶层 verdict 取「真正有数据的那一方」：两个宿主都装着的人，
        如果直接返回 WorkBuddy 的结果，打开 DSH 时会看到一句「没有会话记录」，
        和实际正在用的客户端对不上——这类"看着像失败"的结论最容易让人反复重装。

        `host` 参数用于只查某一家（前端切换宿主时用）。
        """
        from .verify import verify as _wb
        from .verify_dsh import verify as _dsh

        want = (host or "").strip().lower()
        if want in {"workbuddy", "wb"}:
            out = _wb(None, self._identity_files())
            out["host"] = "workbuddy"
            return out
        if want in {"dsh"}:
            return _dsh()

        wb = _wb(None, self._identity_files())
        wb["host"] = "workbuddy"
        try:
            dsh = _dsh()
        except Exception as exc:  # noqa: BLE001
            dsh = {
                "verdict": "no_session",
                "host": "dsh",
                "summary": f"DSH 自检这一步出错了：{exc}",
                "in_context": [],
                "on_disk": [],
            }

        hosts = {"workbuddy": wb, "dsh": dsh}
        # 有真实取证的一方优先；都有就比会话时间，都没有就保持 WorkBuddy 的原结论。
        live = [h for h in (wb, dsh) if h.get("verdict") != "no_session"]
        if not live:
            winner = wb
        elif len(live) == 1:
            winner = live[0]
        else:
            winner = max(live, key=lambda h: float(h.get("session_ts") or 0))

        out = dict(winner)
        out["hosts"] = hosts
        return out

    def audit_report(self, sessions: int = 1, pin: str | None = None) -> dict:
        from .audit import audit as _audit

        return _audit(sessions=sessions, pin=pin)

    def persona_body(self, persona_id: str) -> dict:
        """渲染后的正文（变量用默认值）。云记忆那边要"把这个人格写进去"就用它。"""
        from .persona import PersonaError

        try:
            body = self.library.render(persona_id, {})
        except PersonaError as exc:
            raise ApiError("PERSONA_INVALID", str(exc), 422) from exc
        return {"id": persona_id, "body": body, "chars": len(body)}

    def cloudmem_state(self) -> dict:
        from .cloudmem import list_backups, read

        m = read()
        if m is None:
            return {"exists": False, "backups": []}
        return {"exists": True, **m.as_dict(), "backups": list_backups(8)}

    @serialized
    def cloudmem_write(self, payload: dict) -> dict:
        from .cloudmem import write as _write

        block = str(payload.get("block") or "")
        res = _write(block, force=bool(payload.get("force")))
        _audit(f"cloudmem:write ok={res.get('ok')} chars={len(block)}")
        return res

    @serialized
    def cloudmem_restore(self, payload: dict) -> dict:
        from .cloudmem import restore as _restore

        res = _restore(str(payload.get("path") or ""))
        _audit(f"cloudmem:restore ok={res.get('ok')}")
        # 必须把结果带回去。少了这一句，接口永远回 {"ok": true, "data": null}——
        # 越界路径、备份不存在、写入失败，界面全都显示"还原成功"。
        # 而 _audit 记的是真实结果，于是日志说失败、界面说成功，两边对不上。
        return res

    def budget(self) -> dict:
        """每个有读取窗口的目标：装了多少、还剩多少、你自己的规则能读到多少。

        为什么单独做一个接口：这个约束以前只有"注入了但没反应"时才会被发现。
        变成数字之后，用户注入前就能看见自己还有多少空间。

        算法说明：人格区块都用 prepend 插在开头，所以文件里排在它们之后的就是
        用户自己的内容。可读长度 = min(用户内容长度, 窗口 - 人格占用)。
        """
        from .adapters.base import parse_blocks

        items = []
        for a in all_adapters():
            for t in a.detect().targets:
                if not t.read_limit or not t.path.is_file():
                    continue
                try:
                    raw = t.path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                blocks = parse_blocks(raw)
                persona_chars = sum(len(b.text) for b in blocks)
                user_chars = max(0, len(raw) - persona_chars)
                readable_persona = min(persona_chars, t.read_limit)
                readable_user = max(0, min(user_chars, t.read_limit - readable_persona))
                items.append(
                    {
                        "adapter": a.id,
                        "adapter_name": a.name,
                        "file": t.path.name,
                        "limit": t.read_limit,
                        "total": len(raw),
                        "remaining": t.read_limit - len(raw),
                        "persona_chars": persona_chars,
                        "user_chars": user_chars,
                        "readable_user": readable_user,
                        # 人格还能用多少字符（含即将写入的标记）。
                        # 这个数才是用户真正需要的：窗口不是人格专用，
                        # 得先扣掉他自己的规则和已有标记，剩下的才是人格空间。
                        "persona_budget": max(0, t.read_limit - (len(raw) - persona_chars)),
                        # 标记开销：begin + end 两个注释加换行
                        "markup_chars": max(0, len(raw) - user_chars - persona_chars),
                        # 用户规则是否被挤光了——这是最该让人看见的一条
                        "user_starved": user_chars > 0 and readable_user == 0,
                        "personas": [
                            {"id": b.persona_id, "chars": len(b.text), "state": b.state}
                            for b in blocks
                        ],
                    }
                )
        return {"items": items}

    # ---------------------------------------------------------------- 状态
    def state(self) -> dict:
        # 只调一次 all_adapters：重复构造会反复扫盘，慢且容易读到不一致的状态
        objs = all_adapters()
        adapters = []
        for a in objs:
            raw = a.info()
            raw["hint"] = getattr(a, "hint", "")
            raw["verified"] = getattr(a, "verified", True)
            # 前端据此决定要不要显示「删除」：只有自定义项可删
            raw["custom"] = getattr(a, "is_custom", False)
            adapters.append(raw)
        personas = self.library.discover()
        return {
            "version": VERSION,
            "app": "人格中枢",
            "adapters": adapters,
            "detected_count": sum(1 for a in adapters if a["detected"]),
            "personas": [
                {
                    "id": p.id,
                    "name": p.name,
                    "category": p.category,
                    "desc": p.desc,
                    "chars": len(p.body),
                    "tags": p.tags,
                    "variables": p.variables,
                }
                for p in personas
            ],
            "vault": self.vault.stats(),
            "data_root": str(data_root()),
        }

    # ---------------------------------------------------------------- 人格
    def persona_detail(self, pid: str) -> dict:
        if not self.library.exists(pid):
            raise ApiError("PERSONA_NOT_FOUND", f"人格不存在：{pid}", 404)
        p = self.library.get(pid)
        issues = self.library.validate(pid, {v: "" for v in p.variables})
        issues = [i for i in issues if not i.startswith("变量未提供值")]
        return {
            "id": p.id,
            "name": p.name,
            "category": p.category,
            "desc": p.desc,
            "version": p.version,
            "extends": p.extends,
            "tags": p.tags,
            "body": p.body,
            "chars": len(p.body),
            "variables": p.variables,
            "size_limit": p.size_limit,
            "issues": issues,
            "chain": [c.id for c in self.library.resolve_chain(pid)],
            "source": str(p.source) if p.source else None,
        }

    @serialized
    def persona_delete(self, pid: str) -> dict:
        """删除人格模板。

        **删除前必须先拍快照**：人格是用户一个字一个字写出来的资产，
        早先这里直接 unlink，一次误点（或一次误判的脚本清理）就永久丢失。
        快照进 vault 后，删除变成可还原的操作——「备份与还原」里能看到
        一条 delete-persona:xxx，点还原即可拿回原文件。
        """
        if not self.library.exists(pid):
            # 注释一直写着"映射成 404"，实际走的是 PersonaError → 422。
            # 语义上"要删的东西不存在"是 404，前端按状态码分支会走错。
            raise ApiError("PERSONA_NOT_FOUND", f"人格不存在：{pid}", 404)
        p = self.library.get(pid)
        src = Path(p.source) if p.source else None
        snap_id = ""
        if src is not None and src.is_file():
            snap_id = self.vault.capture([src], label=f"delete-persona:{pid}", actor="gui")
        ok = self.library.delete(pid)
        return {"deleted": ok, "id": pid, "snapshot_id": snap_id}

    @serialized
    def persona_save(self, pid: str, payload: dict, create: bool = False) -> dict:
        """保存人格。

        create=True（新建）与 False（更新）必须分开：
        以前两者走同一条路——get 失败就当成新建、成功就覆盖，
        结果是「新建一个名字生不出英文 id 的人格」会拿到同一个 id（中文名
        slug 后变成 "-"），后建的直接把前一份覆盖。用户实际踩过。
        """
        name = clean_name(payload.get("name"))
        pid = (pid or "").strip()

        if create:
            # 空名 + 空正文 = 一次误点，不该在库里留一份垃圾。
            # 之前两者都空也会建出 persona.md / persona-2.md……，界面上多出一串无名条目，
            # 用户既不知道它们哪来的，也不敢删。
            body_txt = str(payload.get("body") or "").strip()
            if not name and not body_txt:
                raise ApiError("EMPTY_PERSONA", "人格名和正文都是空的，没有可保存的内容", 400)
            # 合法且未占用的 id 直接用；否则由服务端分配一个唯一的
            valid = bool(re.fullmatch(r"[A-Za-z0-9_\-]{1,48}", pid))
            if not valid or self.library.exists(pid):
                pid = self.library.next_free_id(name or "persona")
            p = Persona(id=pid, name=name or pid, body="")
        else:
            if not self.library.exists(pid):
                raise ApiError("PERSONA_NOT_FOUND", f"人格不存在：{pid}", 404)
            p = self.library.get(pid)

        if name:
            p.name = name
        if "body" in payload:
            # 只收字符串。以前是 str(payload["body"])，传数组会被静默存成
            # 字面量 "[1, 2, 3]" 落进人格正文——脏数据一旦落盘，
            # 注入时它就是一个语法正确、内容荒谬的人格，没人知道哪来的。
            raw_body = payload["body"]
            if not isinstance(raw_body, str):
                raise ApiError("BAD_BODY", "人格正文必须是字符串", 400)
            p.body = raw_body
        for key, attr in (("category", "category"), ("desc", "desc")):
            if key in payload:
                if not isinstance(payload[key], str):
                    raise ApiError(f"BAD_{key.upper()}", f"{key} 必须是字符串", 400)
                setattr(p, attr, payload[key])
        if "tags" in payload and isinstance(payload["tags"], list):
            p.tags = [str(t) for t in payload["tags"]]
        if "extends" in payload:
            if payload["extends"] and not isinstance(payload["extends"], str):
                raise ApiError("BAD_EXTENDS", "extends 必须是字符串", 400)
            p.extends = payload["extends"] or None

        path = self.library.save(p)
        return {"id": pid, "path": str(path), "created": create}

    # ---------------------------------------------------------------- 预演
    def preview(self, payload: dict) -> dict:
        persona_id = str(payload.get("persona_id") or "")
        adapter_ids = [str(x) for x in payload.get("adapters") or []]
        values = payload.get("vars") or {}

        if not persona_id:
            raise ApiError("MISSING_PERSONA", "未指定人格")
        if not adapter_ids:
            raise ApiError("MISSING_TARGET", "未指定目标")

        try:
            body = self.library.render(persona_id, values)
        except PersonaError as exc:
            raise ApiError("PERSONA_INVALID", str(exc), 422) from exc

        if not body.strip():
            raise ApiError("PERSONA_EMPTY", "渲染结果为空", 422)

        txn = Transaction(self.vault)
        warn: list[str] = []
        used: list[str] = []

        for aid in adapter_ids:
            adapter = get_adapter(aid)
            if adapter is None:
                warn.append(f"未知目标：{aid}")
                continue
            det = adapter.detect()
            if not det.detected:
                warn.append(f"{adapter.name}：{det.evidence}")
                continue
            # 预览必须用与写入相同的 keep_others，否则「看到的 diff」和「落盘的改动」不是一回事
            res = adapter.compose(
                persona_id, body, keep_others=bool(payload.get("keep_others", True))
            )
            for path, content in res.files.items():
                txn.stage(path, content)
            warn.extend(res.warnings)
            used.append(aid)

        return {
            "persona_id": persona_id,
            "persona_chars": len(body),
            "adapters": used,
            "warnings": warn,
            "files": txn.preview(),
            "total_added": sum(f["added"] for f in txn.preview()),
            "total_removed": sum(f["removed"] for f in txn.preview()),
        }

    @serialized
    def apply(self, payload: dict) -> dict:
        """执行注入。内部是单事务：任一文件失败，全部回滚。"""
        persona_id = str(payload.get("persona_id") or "")
        adapter_ids = [str(x) for x in payload.get("adapters") or []]
        values = payload.get("vars") or {}

        if not persona_id or not adapter_ids:
            raise ApiError("BAD_REQUEST", "需要 persona_id 与 adapters")

        body = self.library.render(persona_id, values)
        # 默认「并存」：已经注入的其他人物保留，同通道各占一个区块。
        # 要"同通道只留这一份"必须显式传 keep_others=false —— 用户报过
        # "注入了新的人格之后，旧的人格又被覆盖了"，默认替换等于悄悄删了他的东西。
        keep_others = bool(payload.get("keep_others", True))
        warnings: list[str] = []
        touched: list[Path] = []
        txn = Transaction(self.vault)
        for aid in adapter_ids:
            adapter = get_adapter(aid)
            if adapter is None:
                continue
            if not adapter.detect().detected:
                continue
            part = adapter.compose(persona_id, body, keep_others=keep_others)
            warnings.extend(part.warnings)
            for path, content in part.files.items():
                txn.stage(path, content)
                touched.append(path)

        result = txn.commit(label=f"apply:{persona_id}", actor="gui")

        # 写成功了 ≠ 已经生效。
        #
        # 这里曾经写过"USER.md 每轮重读、不用重启"——**那个结论是错的**。
        # 依据只是一次观察（清掉标记后下一轮上下文跟着变了），用户实测推翻了它：
        # 注入后不重启 WorkBuddy，人格没生效，而且 identity_context 后面几轮压根不出现。
        #
        # 现在按事实说话：身份文件是会话级注入，宿主何时重读不可靠；
        # 真正"每轮注入"的是账号级云记忆 memoryBlock（见 README 的逆向对照），
        # 而那条通道本工具目前只检测、不写入。
        hosts = host_processes()
        if result.written:
            names = "、".join(sorted({p.name for p in touched}))
            tail = (
                "**请完全退出 WorkBuddy（含右下角托盘图标）后重开**，再确认一次人格是否生效。"
            )
            if hosts:
                who = "、".join(f"{k}（{v} 个进程）" for k, v in hosts.items())
                warnings.append(
                    f"已写入 {names}，但检测到 {who} 正在运行。宿主何时重读身份文件不可靠，"
                    + tail
                )
            else:
                warnings.append(f"已写入 {names}。" + tail)

        return {
            "ok": result.ok,
            "snapshot_id": result.snapshot_id,
            "message": result.message,
            "written": result.written,
            "steps": [{"path": s.path, "status": s.status, "detail": s.detail} for s in result.steps],
            "warnings": warnings,
            "host_running": hosts,
        }

    @serialized
    def cleanup(self, payload: dict) -> dict:
        """摘除注入：把标记区块去掉，并删除本工具新建的托管文件。"""
        adapter_ids = [str(x) for x in payload.get("adapters") or []]
        if not adapter_ids:
            raise ApiError("BAD_REQUEST", "需要 adapters")

        txn = Transaction(self.vault)
        for aid in adapter_ids:
            adapter = get_adapter(aid)
            if adapter is None:
                continue
            det = adapter.detect()
            res = adapter.cleanup(det.targets)
            for path, content in res.files.items():
                txn.stage(path, content if content != "" else None)

        result = txn.commit(label="cleanup", actor="gui")
        return {
            "ok": result.ok,
            "snapshot_id": result.snapshot_id,
            "message": result.message,
            "steps": [{"path": s.path, "status": s.status, "detail": s.detail} for s in result.steps],
        }

    # ---------------------------------------------------------------- 注入检测 / 开关
    def injections(self) -> dict:
        from .detect import scan_all, summarize

        items = scan_all()
        return {"items": [f.as_dict() for f in items], "summary": summarize(items)}

    @serialized
    def toggle_injection(self, payload: dict) -> dict:
        from .switch import toggle

        fid = str(payload.get("id") or "")
        if not fid:
            raise ApiError("BAD_REQUEST", "需要 id")
        return toggle(self.vault, self.library, fid, bool(payload.get("enable")))

    @serialized
    def remove_injections(self, payload: dict) -> dict:
        from .switch import remove

        ids = [str(x) for x in (payload.get("ids") or [])]
        return remove(self.vault, ids)

    # ---------------------------------------------------------------- 在线升级
    def update_check(self, source: str | None) -> dict:
        from .update import DEFAULT_SOURCE, check_update, pending

        info = check_update(source or DEFAULT_SOURCE)
        data = info.as_dict()
        data["pending"] = pending()
        return data

    @serialized
    def update_download(self, payload: dict) -> dict:
        from .update import DEFAULT_SOURCE, UpdateError, check_update, download_and_stage

        info = check_update(str(payload.get("source") or DEFAULT_SOURCE))
        if info.error and not info.has_update:
            raise ApiError("UPDATE_CHECK_FAILED", info.error)
        try:
            return download_and_stage(info)
        except UpdateError as exc:
            raise ApiError(exc.code, exc.message) from exc

    # ---------------------------------------------------------------- 体检
    def doctor(self) -> dict:
        """只读体检：只统计、不写任何文件。

        对应破甲的「演练 / --dry-run」：动手前先看一遍现状。
        这里额外算两件它不算的事——通道上限、宿主是否还在运行。
        """
        from .detect import scan_all, summarize
        from .update import pending

        objs = all_adapters()
        channels = []
        for a in objs:
            det = a.detect()
            for t in det.targets:
                channels.append(
                    {
                        "adapter": a.id,
                        "adapter_name": a.name,
                        "file": t.path.name,
                        "path": str(t.path),
                        "exists": t.path.is_file(),
                        "insert_at": t.insert_at,
                        "read_limit": t.read_limit,
                        "note": t.note,
                    }
                )

        hosts = host_processes()
        items = scan_all()
        return {
            "version": VERSION,
            "data_root": str(data_root()),
            "hosts_running": hosts,
            "restart_needed": bool(hosts),
            "adapters": {
                "total": len(objs),
                "detected": sum(1 for a in objs if a.detect().detected),
            },
            "channels": channels,
            "injections": summarize(items),
            "vault": self.vault.stats(),
            "pending_update": pending(),
            "tpl": self._tpl_status_safe(),
            "notes": [
                "本次为只读体检：未写入、未备份、未改动任何文件",
                "MEMORY.md 的读取窗口实测约 4000 字符；云记忆 <memory> 区块约 10000 字符",
                "客户端运行中改动不会生效，需完全退出（含托盘）后重开",
            ],
        }

    # ---------------------------------------------------------------- 宿主模板
    def _tpl_status_safe(self) -> dict:
        """模板状态。体检要的是"看见"，不是"因此挂掉"。

        宿主没装、路径不同、文件权限特殊，都只该变成一条 absent，
        不该让整个 doctor 500——体检本身失败比查不出问题更糟。
        """
        try:
            from .tplpatch import status as _status

            return _status()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}", "expert": {"state": "unknown"},
                    "warnings": ["模板状态读取失败，见 detail"]}

    def tpl_status(self) -> dict:
        from .tplpatch import status as _status

        return _status()

    @serialized
    def tpl_apply(self) -> dict:
        from .tplpatch import TplError, patch as _patch

        try:
            return _patch()
        except TplError as exc:
            raise ApiError("TPL_FAILED", str(exc), 422) from exc

    @serialized
    def tpl_revert(self) -> dict:
        from .tplpatch import TplError, revert as _revert

        try:
            return _revert()
        except TplError as exc:
            raise ApiError("TPL_FAILED", str(exc), 422) from exc

    # ---------------------------------------------------------------- 快照
    def snapshots(self) -> dict:
        return {
            "items": [
                {
                    "id": m.id,
                    "time": m.time_str,
                    "label": m.label,
                    "actor": m.actor,
                    "files": len(m.entries),
                    "bytes": sum(e.get("size") or 0 for e in m.entries),
                }
                for m in self.vault.list_snapshots()
            ],
            "stats": self.vault.stats(),
        }

    @serialized
    def restore(self, payload: dict) -> dict:
        sid = str(payload.get("snapshot_id") or "")
        if not sid:
            raise ApiError("BAD_REQUEST", "需要 snapshot_id")
        results = self.vault.restore(sid)
        return {
            "ok": all(r.ok for r in results),
            "items": [{"path": r.path, "action": r.action, "ok": r.ok, "detail": r.detail} for r in results],
        }

    def verify(self, sid: str) -> dict:
        results = self.vault.verify(sid)
        return {
            "ok": all(r.ok for r in results),
            "items": [{"path": r.path, "state": r.action, "ok": r.ok} for r in results],
        }

    @serialized
    def gc(self) -> dict:
        removed, freed = self.vault.gc(keep=30)
        return {"removed_snapshots": removed, "freed_bytes": freed, "stats": self.vault.stats()}

    def logs(self, limit: int = 200) -> dict:
        p = log_path()
        if not p.is_file():
            return {"lines": []}
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"lines": lines[-limit:][::-1]}

    # ---------------------------------------------------------------- 自定义目标
    @serialized
    def custom_add(self, payload: dict) -> dict:
        from .adapters import add_custom

        paths = [str(x) for x in (payload.get("paths") or []) if str(x).strip()]
        if not paths:
            raise ApiError("BAD_REQUEST", "至少需要一个文件路径")
        try:
            a = add_custom(str(payload.get("name") or ""), paths, str(payload.get("id") or ""))
        except ValueError as exc:
            raise ApiError("BAD_REQUEST", str(exc)) from exc
        return {
            "id": a.id,
            "name": a.name,
            "paths": [str(p) for p in a._candidates],
            "count": len(paths),
        }

    @serialized
    def custom_remove(self, payload: dict) -> dict:
        from .adapters import remove_custom

        cid = str(payload.get("id") or "").strip()
        if not cid:
            raise ApiError("BAD_REQUEST", "需要 id")
        if not remove_custom(cid):
            raise ApiError("NOT_FOUND", f"没有这个自定义目标：{cid}", 404)
        # 只删配置，不动已经写进目标文件的内容——那属于用户的真实文件，
        # 要清除请去「注入管理」，那里有存档可还原。
        return {"removed": cid}


# --------------------------------------------------------------------- HTTP 层
class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = f"PersonaHub/{VERSION}"

    # 静音默认的 stderr 访问日志（GUI 场景下刷屏无意义）
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    # ---------------------------------------------------------------- 工具
    def _drain_body(self) -> None:
        """把"这次没读过"的请求体读掉再回响应。

        为什么要这一步：路由没匹配上、方法不对、Content-Type 被拒时，我们没读 body
        就直接回错误。客户端此时还在往 socket 里写，我们先 close，Windows 上直接 RST，
        客户端拿到的是 ConnectionAborted / `Failed to fetch` —— 而真实原因只是 404 或 415。
        实测（200KB 中文正文 = 约 600KB）：
            PUT /api/nope   -> ConnectionAbortedError
            PUT /api/apply  -> ConnectionAbortedError（apply 只收 POST，走的就是 404 分支）
            PUT /api/personas/zzz -> 正常 404（body 小，恰好塞进了 socket 缓冲区）
        也就是说这是竞态：body 一大就必现。

        上限 4MB：再大的请求体不再读，直接关连接。读掉它只是为了把错误信息送到，
        不值得为此让一个线程去吞几百 MB。
        """
        if getattr(self, "_body_consumed", False):
            return
        self._body_consumed = True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return
        if n > 4 * 1024 * 1024 or n > MAX_BODY_BYTES:
            self.close_connection = True
            return
        try:
            self.rfile.read(n)
        except (OSError, ValueError):
            self.close_connection = True

    def _send(self, status: int, payload: dict | bytes, ctype: str = "application/json; charset=utf-8") -> None:
        # 必须在 send_response 之前：一旦响应头发出，客户端就开始读，草稿就晚了。
        self._drain_body()
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 这里曾经发 `Access-Control-Allow-Origin: null`，注释写的是"只允许本机页面调用"。
        # 那行是错的，而且方向相反：`null` 正是 sandboxed iframe 的 origin，等于对
        # 任何 `<iframe sandbox>` 打开放行，恶意页面可以把它嵌进去读本机状态再 postMessage 出来。
        # 页面是本服务自己发的，同源，不需要 CORS —— 一个都不发才是对的。
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _ok(self, data) -> None:
        self._send(HTTPStatus.OK, {"ok": True, "data": data})

    def _err(self, code: str, message: str, status: int = 400) -> None:
        self._send(status, {"ok": False, "error": {"code": code, "message": message}})

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n > MAX_BODY_BYTES:
            # 不能只报错就返回：客户端还在发数据，不关连接就会一直 block 住。
            # 这个分支刻意不 drain（_body_consumed 置位让 _drain_body 直接跳过）：
            # 16MB 以上不值得为了一句错误提示去吞。
            self._body_consumed = True
            self.close_connection = True
            raise ApiError("TOO_LARGE", f"请求体 {n} 字节，超过上限 {MAX_BODY_BYTES}", 413)
        if n <= 0:
            return {}
        # CSRF 闸门：跨站页面只能发"简单请求"（text/plain、表单），设不了
        # application/json —— 设了就得先过 preflight，而本服务不返回任何 CORS 头，
        # preflight 必然失败。所以卡死 Content-Type，等于把跨站写操作这条路堵掉。
        # 只认 127.0.0.1 挡不住这个：请求确实来自本机浏览器，Host 也确实是 127.0.0.1。
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError("BAD_CONTENT_TYPE",
                           f"只接受 application/json（收到 {ctype or '空'}）", 415)
        self._body_consumed = True
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            val = json.loads(raw.decode("utf-8"))
            return val if isinstance(val, dict) else {"value": val}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError("BAD_JSON", f"请求体不是合法 JSON: {exc}") from exc

    # ---------------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    def _route(self, method: str) -> None:
        # 页面存活心跳：header 侧刷新时间戳，供外壳进程判断窗口是否已关
        if hasattr(self.server, "last_request"):
            self.server.last_request = time.time()  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Host 校验：只绑 127.0.0.1 挡不住 DNS rebinding（恶意域名解析到 127.0.0.1
        # 后浏览器认为是同源）。所以看 Host 头，不看连接来源。
        host_hdr = self.headers.get("Host") or ""
        host_only = (host_hdr.split("]")[0] + "]") if host_hdr.startswith("[") else host_hdr.split(":")[0]
        if host_only and host_only.strip().lower() not in ALLOWED_HOSTS:
            return self._err("FORBIDDEN_HOST",
                             f"只接受本机访问（Host: {host_hdr}）。"
                             f"如果这是你自己配置的反向代理，请直接用 127.0.0.1 访问。", 403)

        try:
            if not path.startswith("/api/"):
                self._static(path)
                return

            parts = [p for p in path.strip("/").split("/") if p][1:]  # 去掉 'api'
            app = self.app

            # --- 状态
            if parts == ["ping"]:
                # 极轻量心跳，供外壳进程判活，不触发任何磁盘扫描。
                # 带上 app/version：入口进程靠它区分「本程序的实例」和「别人的服务占了这个端口」，
                # 否则只能靠能不能解析 JSON 来猜，猜错了就会把无关服务当成自己。
                if (query.get("page") or [""])[0] == "1":
                    app.last_page_ping = time.time()
                idle = (time.time() - app.last_page_ping) if app.last_page_ping else None
                return self._ok({
                    "t": time.time(), "app": APP_NAME_CN, "version": VERSION,
                    "page_alive": bool(idle is not None and idle <= PAGE_ALIVE_WINDOW),
                    "page_idle": None if idle is None else round(idle, 1),
                })
            if parts == ["bye"]:
                # 页面卸载时的主动通知（sendBeacon）。不读 body，容忍任何 Content-Type。
                self.app.closing = True
                return self._ok({"bye": True})
            if parts == ["state"]:
                return self._ok(app.state())
            if parts == ["doctor"] and method == "GET":
                return self._ok(app.doctor())
            if parts == ["budget"] and method == "GET":
                return self._ok(app.budget())
            # 生效自检：从宿主自己的会话记录里读回"这一轮到底读到了什么"
            # ?host=dsh / workbuddy 只查一家；不传则两家都查
            if parts == ["verify"] and method == "GET":
                return self._ok(app.verify_injection((query.get("host") or [None])[0]))
            # 破甲度：注入进去之后起了多少作用（只统计输出，不诱导内容）
            if parts == ["audit"] and method == "GET":
                n = 1
                try:
                    n = max(1, min(10, int((query.get("sessions") or ["1"])[0])))
                except (TypeError, ValueError):
                    n = 1
                # 会话名要拼进 glob，必须白名单：UUID + .jsonl，别的字符一律不认。
                # 不校验就等于把"通配符"交给调用方，`*` 能匹配到别的会话文件。
                pin = (query.get("session") or [""])[0].strip()
                if pin and not re.fullmatch(r"[0-9A-Za-z_\-]{1,64}\.jsonl", pin):
                    raise ApiError("BAD_SESSION", f"会话名不合法：{pin[:40]}", 400)
                return self._ok(app.audit_report(n, pin or None))
            if parts == ["cloudmem"]:
                if method == "GET":
                    return self._ok(app.cloudmem_state())
                if method == "POST":
                    return self._ok(app.cloudmem_write(self._body()))
            if parts == ["cloudmem", "restore"] and method == "POST":
                return self._ok(app.cloudmem_restore(self._body()))
            if parts == ["adapters"]:
                return self._ok([a.info() for a in all_adapters()])
            # 宿主模板：专家会话读不读得到 SOUL，全靠这一处补插槽
            if parts == ["tpl"] and method == "GET":
                return self._ok(app.tpl_status())
            if parts == ["tpl", "apply"] and method == "POST":
                return self._ok(app.tpl_apply())
            if parts == ["tpl", "revert"] and method == "POST":
                return self._ok(app.tpl_revert())
            if parts == ["logs"]:
                return self._ok(app.logs())

            # --- 人格
            if parts == ["personas"] and method == "GET":
                return self._ok(app.state()["personas"])
            if parts == ["personas"] and method == "POST":
                body = self._body()
                return self._ok(app.persona_save(str(body.get("id") or ""), body, create=True))
            if len(parts) == 2 and parts[0] == "personas" and method == "GET":
                return self._ok(app.persona_detail(parts[1]))
            # 只取渲染后的正文：给「把某个人格写进云记忆」用，不落盘
            if len(parts) == 3 and parts[0] == "personas" and parts[2] == "body" and method == "GET":
                return self._ok(app.persona_body(parts[1]))
            if len(parts) == 2 and parts[0] == "personas" and method == "PUT":
                return self._ok(app.persona_save(parts[1], self._body()))
            if len(parts) == 2 and parts[0] == "personas" and method == "DELETE":
                return self._ok(app.persona_delete(parts[1]))
            if parts == ["personas", "import"] and method == "POST":
                body = self._body()
                src = Path(str(body.get("path") or ""))
                if not src.is_file():
                    raise ApiError("NOT_FOUND", f"文件不存在: {src}", 404)
                dst = app.library.import_file(src, body.get("id"), body.get("name"))
                return self._ok({"id": dst.stem, "path": str(dst)})

            # --- 注入
            if parts == ["preview"] and method == "POST":
                return self._ok(app.preview(self._body()))
            if parts == ["apply"] and method == "POST":
                return self._ok(app.apply(self._body()))
            if parts == ["cleanup"] and method == "POST":
                return self._ok(app.cleanup(self._body()))

            # --- 注入检测与开关
            if parts == ["injections"] and method == "GET":
                return self._ok(app.injections())
            if parts == ["injections", "toggle"] and method == "POST":
                return self._ok(app.toggle_injection(self._body()))
            if parts == ["injections", "remove"] and method == "POST":
                return self._ok(app.remove_injections(self._body()))

            # --- 快照
            if parts == ["snapshots"] and method == "GET":
                return self._ok(app.snapshots())
            if len(parts) == 3 and parts[0] == "snapshots" and parts[2] == "verify":
                return self._ok(app.verify(parts[1]))
            if parts == ["restore"] and method == "POST":
                return self._ok(app.restore(self._body()))
            if parts == ["vault", "gc"] and method == "POST":
                return self._ok(app.gc())

            # --- 自定义目标
            if parts == ["custom"] and method == "POST":
                return self._ok(app.custom_add(self._body()))
            if parts == ["custom", "remove"] and method == "POST":
                return self._ok(app.custom_remove(self._body()))

            # --- 在线升级
            if parts == ["update", "check"] and method == "GET":
                return self._ok(app.update_check((query.get("source") or [None])[0]))
            if parts == ["update", "download"] and method == "POST":
                return self._ok(app.update_download(self._body()))

            raise ApiError("NOT_FOUND", f"未知接口: {method} {path}", 404)

        except ApiError as exc:
            self._err(exc.code, exc.message, exc.status)
        except PersonaError as exc:
            self._err("PERSONA_INVALID", str(exc), 422)
        except FileNotFoundError as exc:
            self._err("NOT_FOUND", str(exc), 404)
        except Exception as exc:  # noqa: BLE001
            # switch 模块的业务错误同样按稳定错误码返回，不让它落到 INTERNAL
            code = getattr(exc, "code", None)
            if code:
                return self._err(code, getattr(exc, "message", str(exc)), 400)
            detail = traceback.format_exc(limit=4)
            self._err("INTERNAL", f"{type(exc).__name__}: {exc}", 500)
            try:
                with open(log_path(), "a", encoding="utf-8") as fh:
                    fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] {self.path}\n{detail}\n")
            except OSError:
                pass

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        # 防目录穿越：结果必须仍在 web/ 之内
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._err("FORBIDDEN", "非法路径", 403)
        if not target.is_file():
            return self._err("NOT_FOUND", f"资源不存在: {rel}", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)


class Server:
    def __init__(self, port: int = DEFAULT_PORT):
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.app: App | None = None
        # 最近一次页面请求的时间（兼作进程异常退出时的兜底判据）
        self.last_request: float = time.time()

    @staticmethod
    def _port_busy(p: int) -> bool:
        """这个端口上是不是已经有东西在 LISTEN。

        为什么必须主动探一次，不能只靠 bind 失败来判断：
        `HTTPServer.allow_reuse_address = True`（标准库默认）会把 SO_REUSEADDR 打上，
        而 **Windows 的 SO_REUSEADDR 语义和 Unix 不同**——它允许两个进程同时绑定同一端口，
        bind 直接成功、不报错。实测踩到过：两个实例同时 LISTEN 8799，
        请求随机落到其中一个，用户看到的是"注入成功但另一个窗口还是旧状态"。

        bind 失败这套判据在 Windows 上因此是失效的，只能补一次 TCP 连接探测。
        TIME_WAIT 的连接不接受 SYN，所以不会把"刚关掉"误判成"还开着"。
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            return s.connect_ex(("127.0.0.1", p)) == 0

    def start(self) -> int:
        app = App()
        self.app = app          # 对外暴露，供入口进程查询 closing
        handler = type("BoundHandler", (Handler,), {"app": app})
        # 端口有东西在听就往后找 20 个，避免与其他本地服务撞车
        last_exc: Exception | None = None
        for p in range(self.port, self.port + 20):
            if self._port_busy(p):
                last_exc = OSError(f"端口 {p} 已被占用")
                continue
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", p), handler)
                self.httpd.daemon_threads = True
                self.port = p
                break
            except OSError as exc:
                last_exc = exc
                continue
        if self.httpd is None:
            raise RuntimeError(f"无法绑定本地端口（{self.port}–{self.port + 19}）: {last_exc}")

        self._thread = threading.Thread(target=self.httpd.serve_forever, name="hub-http", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
