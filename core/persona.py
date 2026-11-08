"""人格模板库。

破甲一键通只有「一套写死的 9,764 字人格 + 一个云端下发的容器」。
本模块把它做成可组合的体系：

  - 一个模板 = 一个 .md 文件，带 frontmatter 元数据，正文即人格
  - `extends` 支持继承：基础人格 + 领域扩展，避免每套都复制一遍通用条款
  - `{{变量}}` 支持参数化：同一套人格可产出不同语气/尺度/角色变体
  - 校验：必填字段、变量未闭合、继承环、体积上限，一次性报全
  - 导入：任意现成 .md（比如破甲自己那份）直接收编成模板

刻意用 Markdown + 极简 frontmatter，而不是 YAML：
标准库没有 YAML 解析器，为了一份配置引入第三方依赖不值得。
"""
from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_\-]+)\s*\}\}")
FRONT_KEYS = {"id", "name", "category", "tags", "version", "extends", "desc", "author", "size_limit"}

# 单套人格的软上限。宿主普遍有注入上限（WorkBuddy 记忆块约 10k 字），
# 超了不是「能存进去」就行——超出部分会被截断，等于静默失效。
# 单个人格正文的「卫生线」，**不是宿主限制**。
#
# 这里原本写 9000，注释还说"超限会被宿主丢弃"——那个说法是编的，没有任何依据。
# 真实约束在**目标**一侧：每个落点有自己的读取窗口，且窗口里要装的东西
# 不止人格，还包括用户自己写的规则（见 adapters/base.py 的 Target.read_limit）。
#
# 实测：WorkBuddy 的 MEMORY.md 只有前 4,000 字符会被注入，而用户自己的规则
# 也在同一个 4,000 里。所以"单个人格 9000 字"这个概念本身就是错的——
# 它既不是宿主限制，也不反映多人格累加后的挤占。
#
# 改成 0 = 默认不做这项检查。要防手滑，自己在 frontmatter 写 size_limit。
DEFAULT_SIZE_LIMIT = 0


def _decode_text(src: Path) -> tuple[str, str]:
    """按「UTF-8 → GB18030」顺序解码，返回 (文本, 用到的编码名)。

    顺序不能反：GB18030 几乎能解任何字节流，先试它会把合法的 UTF-8
    中文解成乱码，而且不报错。UTF-8 严格解失败才说明它真不是 UTF-8。

    最后一道 replace 兜底是给"既不是 UTF-8 也不是 GB18030"的文件留的活路，
    但那种情况下正文会是可见的替换字符，用户能立刻看出不对，
    不会像以前那样显示成一片诡异的方块还以为是自己写错了。
    """
    raw = src.read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc), "UTF-8"
        except UnicodeDecodeError:
            continue
    try:
        return raw.decode("gb18030"), "GBK"
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), "未知编码"


class PersonaError(ValueError):
    pass


# ---------------------------------------------------------------- frontmatter
def parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 `---` 包裹的极简 frontmatter。只支持 key: value 与 [a, b]。"""
    if not text.startswith("---"):
        return {}, text

    lines = text.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text

    meta: dict = {}
    for line in lines[1:end]:
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip("\"'") for x in v[1:-1].split(",") if x.strip()]
        else:
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            meta[k] = v

    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return meta, body


def dump_frontmatter(meta: dict, body: str) -> str:
    out = ["---"]
    for k in ("id", "name", "category", "version", "extends", "desc", "author"):
        if meta.get(k):
            out.append(f"{k}: {meta[k]}")
    if meta.get("tags"):
        out.append(f"tags: [{', '.join(meta['tags'])}]")
    if meta.get("size_limit"):
        out.append(f"size_limit: {meta['size_limit']}")
    out.append("---")
    out.append("")
    return "\n".join(out) + body.rstrip() + "\n"


# ---------------------------------------------------------------- 数据模型
@dataclass
class Persona:
    id: str
    name: str
    body: str
    category: str = "通用"
    tags: list[str] = field(default_factory=list)
    version: str = "1.0.0"
    extends: str | None = None
    desc: str = ""
    author: str = ""
    size_limit: int = DEFAULT_SIZE_LIMIT
    source: Path | None = None

    @property
    def variables(self) -> list[str]:
        seen: list[str] = []
        for m in VAR_RE.finditer(self.body):
            v = m.group(1)
            if v not in seen:
                seen.append(v)
        return seen

    def render(self, values: dict[str, str] | None = None) -> str:
        vals = dict(values or {})

        def sub(m: re.Match) -> str:
            k = m.group(1)
            # 未提供变量时保留原样，便于在校验阶段统一报告，而不是静默变空串
            return vals.get(k, m.group(0))

        return VAR_RE.sub(sub, self.body)


# ---------------------------------------------------------------- 库
class PersonaLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- 读取
    def discover(self) -> list[Persona]:
        out: list[Persona] = []
        for p in sorted(self.root.glob("**/*.md")):
            try:
                out.append(self._load_file(p))
            except Exception:
                continue
        return out

    def _load_file(self, path: Path) -> Persona:
        text = path.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        pid = str(meta.get("id") or path.stem)
        tags = meta.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        try:
            limit = int(meta.get("size_limit") or DEFAULT_SIZE_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_SIZE_LIMIT
        return Persona(
            id=pid,
            name=str(meta.get("name") or pid),
            body=body,
            category=str(meta.get("category") or "通用"),
            tags=list(tags),
            version=str(meta.get("version") or "1.0.0"),
            extends=str(meta["extends"]) if meta.get("extends") else None,
            desc=str(meta.get("desc") or ""),
            author=str(meta.get("author") or ""),
            size_limit=limit,
            source=path,
        )

    def get(self, pid: str) -> Persona:
        for p in self.discover():
            if p.id == pid:
                return p
        raise PersonaError(f"人格不存在: {pid}")

    def exists(self, pid: str) -> bool:
        """判断 id 是否被占用。

        两路都要看：
          1) 文件名 —— 快路径，正常情况 id 与文件名一致
          2) frontmatter 的 id —— 用户手工改过文件名时，只看文件名会判重失效，
             新建就会撞上已有的人格并把它覆盖。

        早先只查文件名，注释里还写着"正常路径下一致所以不值得看 frontmatter"。
        那条假设在用户手动整理过文件之后就不成立了，而代价是**静默覆盖用户数据**，
        所以宁可多解析几个文件。
        """
        if not pid:
            return False
        if any(p.stem == pid for p in self.root.glob("**/*.md")):
            return True
        return any(p.id == pid for p in self.discover())

    def next_free_id(self, base: str = "persona") -> str:
        """给一个未被占用的 id。

        中文名 slug 之后会变成空串或单个 "-"——实测踩过：两个中文名都变成 "-"，
        于是后建的那份把前一份覆盖掉了。这里必须兜底成 persona / persona-2 …
        """
        slug = re.sub(r"[^a-z0-9_-]+", "-", (base or "").lower()).strip("-")
        if not slug:
            slug = "persona"
        if not self.exists(slug):
            return slug
        n = 2
        while self.exists(f"{slug}-{n}"):
            n += 1
        return f"{slug}-{n}"

    # ---------------------------------------------------------- 继承链
    def resolve_chain(self, pid: str) -> list[Persona]:
        """返回 [最底层祖先, ..., 自身]。检测继承环。"""
        chain: list[Persona] = []
        seen: set[str] = set()
        cur = pid
        while cur:
            if cur in seen:
                raise PersonaError(f"继承环: {' -> '.join(p.id for p in chain)} -> {cur}")
            seen.add(cur)
            p = self.get(cur)
            chain.insert(0, p)
            cur = p.extends or ""
        return chain

    def render(self, pid: str, values: dict[str, str] | None = None) -> str:
        """渲染继承链：祖先正文在前，后代在后。"""
        parts: list[str] = []
        for p in self.resolve_chain(pid):
            text = p.render(values).strip()
            if text:
                parts.append(text)
        return "\n\n".join(parts)

    # ---------------------------------------------------------- 校验
    def validate(self, pid: str, values: dict[str, str] | None = None) -> list[str]:
        issues: list[str] = []
        try:
            chain = self.resolve_chain(pid)
        except PersonaError as exc:
            return [str(exc)]

        me = chain[-1]
        if not me.name.strip():
            issues.append("缺少 name")
        if not me.body.strip():
            issues.append("正文为空")

        try:
            text = self.render(pid, values)
        except PersonaError as exc:
            return issues + [str(exc)]

        # 未提供值的变量单独列出——静默替换成空串会让注入内容莫名缺段
        missing = sorted({m.group(1) for m in VAR_RE.finditer(text)})
        if missing:
            issues.append(f"变量未提供值: {', '.join(missing)}")

        # 只在人格自己声明了 size_limit 时才检查。
        # 这不是宿主限制——真正的约束是目标文件的读取窗口，由 adapter 在注入时结算。
        if me.size_limit and len(text) > me.size_limit:
            issues.append(
                f"渲染后 {len(text)} 字，超过本模板自设的 size_limit {me.size_limit} 字"
                f"（这是你自己设的卫生线，不是宿主上限）"
            )

        if not text.strip():
            issues.append("渲染结果为空")
        return issues

    # ---------------------------------------------------------- 写入
    def create(self, pid: str, name: str, body: str = "", category: str = "通用", desc: str = "") -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,48}", pid):
            raise PersonaError("id 只能用小写字母、数字、下划线和短横线，长度 1–48")
        dst = self.root / f"{pid}.md"
        if dst.exists():
            raise PersonaError(f"已存在: {dst}")
        dst.write_text(
            dump_frontmatter(
                {"id": pid, "name": name, "category": category, "version": "1.0.0", "desc": desc},
                body or f"# {name}\n\n（在此写下人格正文）\n",
            ),
            encoding="utf-8",
            newline="\n",   # 固定 LF：文本模式在 Windows 上会把 \n 改写成 \r\n
        )
        return dst

    def save(self, persona: Persona) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,48}", persona.id):
            raise PersonaError(f"非法 id：{persona.id!r}")
        path = persona.source or (self.root / f"{persona.id}.md")
        path.write_text(
            dump_frontmatter(
                {
                    "id": persona.id,
                    "name": persona.name,
                    "category": persona.category,
                    "version": persona.version,
                    "desc": persona.desc,
                    "author": persona.author,
                    "extends": persona.extends,
                    "tags": persona.tags,
                    "size_limit": persona.size_limit
                    if persona.size_limit != DEFAULT_SIZE_LIMIT
                    else None,
                },
                persona.body,
            ),
            encoding="utf-8",
            newline="\n",   # 固定 LF：文本模式在 Windows 上会把 \n 改写成 \r\n
        )
        return path

    def delete(self, pid: str) -> bool:
        p = self.get(pid)
        if p.source and p.source.is_file():
            p.source.unlink()
            return True
        return False

    def import_file(self, src: Path, pid: str | None = None, name: str | None = None) -> Path:
        """把任意现成 .md 收编为模板（例如破甲自己那份 persona.md）。

        编码必须兜住 GBK：原来是无条件 `utf-8 + errors="replace"`，
        一份 GB18030 的旧笔记导进来，每个汉字都变成 U+FFFD，
        界面显示一串方框，而**接口返回 200、不报任何错**。
        用户看不出是编码问题，只会认为"导入坏了、人格写不进去"。
        中文 Windows 上用记事本、Word 另存、旧下载站拿到的 .md 大量是 GBK。
        """
        src = Path(src)
        text, enc = _decode_text(src)
        meta, body = parse_frontmatter(text)
        if not body.strip():
            body, meta = text, {}
        if not body.strip():
            # 空文件导进来会变成一条正文为空的人格，界面上多个点不开的空条目。
            raise PersonaError(f"{src.name} 里没有任何正文（0 字），没有可导入的内容")

        new_id = re.sub(r"[^A-Za-z0-9_\-]", "-", str(pid or src.stem)).strip("-").lower()
        if not re.fullmatch(r"[a-z0-9_\-]{1,48}", new_id):
            # 调用方传进来的 id 以前直接拼进路径 —— 传 "../../x" 就能把文件
            # 写到人格库外面（实测可写出 <home>/escaped.md）。这里统一兜底。
            new_id = "imported"
        dst = self.root / f"{new_id}.md"
        n = 2
        while dst.exists():
            dst = self.root / f"{new_id}-{n}.md"
            n += 1

        # 非 UTF-8 来源要写在 desc 里：不然用户看到的是一份"正常导入"的人格，
        # 完全不知道它被转过码，日后对不上源文件时无从查起。
        enc_note = "" if enc == "UTF-8" else f"，按 {enc} 解码"

        dst.write_text(
            dump_frontmatter(
                {
                    "id": dst.stem,
                    "name": name or str(meta.get("name") or src.stem),
                    "category": str(meta.get("category") or "导入"),
                    "version": "1.0.0",
                    "desc": f"由 {src.name} 导入（{len(body)} 字{enc_note}）",
                    "author": str(meta.get("author") or ""),
                },
                body,
            ),
            encoding="utf-8",
            newline="\n",   # 固定 LF：文本模式在 Windows 上会把 \n 改写成 \r\n
        )
        return dst
