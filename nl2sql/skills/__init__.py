"""技能加载：扫描技能根目录、解析 frontmatter、产出只读目录与报告。

与 `plugins/loader.py` 同构，但有三处**刻意的差别**：

| | 插件 | 技能 |
|---|---|---|
| 内容 | 可执行代码（`plugin.py`） | **纯文本**（`SKILL.md`） |
| 默认 | 外部一律不加载 | 项目 `skills/` 默认加载，外部需点名 |
| 失败 | 记进 `errors`，不阻断启动 | 同上，且**必须能被看见**（见 `SkillLoadReport`） |

技能比插件**安全**（没有代码执行），但它进的是模型上下文，所以是一类
prompt 注入面——信任模型照抄插件那条"默认不运行别人的代码"，
平移成"默认不加载别人的指令"。

为什么默认加载项目技能而默认不加载外部技能
------------------------------------------
* `skills/`（项目内）：与代码同源、同一次评审，等价于"我们自己的 prompt"。
* `.dsh/skills` / `.agents/skills` / `~/.dsh/skills`（外部）：可能是
  `npx skills add` 装进来的、给**别的 agent** 看的技能（比如 `find-skills`
  教 agent 怎么搜技能）。默认灌进我们的查询规划器既跑题又白烧 token。

失败必须可见
------------
`SkillLoadReport` 记录 `loaded / skipped / rejected / warnings`。
参考 `docs/08` 第 8 节：**路径写错不能静默回退**——
`_load_decision_prompt()` 原来的 `return ""` 就是反面教材。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..contracts.skill import (
    DESCRIPTION_MAX_CHARS,
    FRONTMATTER_MAX_CHARS,
    VALID_KINDS,
    SkillSpec,
)
from ..errors import ConfigError

__all__ = [
    "SkillError",
    "SkillLoadReport",
    "SkillLoader",
    "LoadedSkillCatalog",
    "split_frontmatter",
    "DEFAULT_EXTERNAL_ROOTS",
]

# 外部技能根目录。默认**不加载**（见模块 docstring）。
# 前两个是项目级的（相对工作目录），第三个是用户级的。
DEFAULT_EXTERNAL_ROOTS = (".dsh/skills", ".agents/skills", "~/.dsh/skills")

# 技能名只允许字母数字与连字符（与生态一致）
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)

# 正文上限：防止一份超长技能把上下文吃光。
# 这不是"截断"——超限是**拒绝加载**并报原因，因为截断一份流程等于毁掉它。
DEFAULT_MAX_BODY_CHARS = 20000


class SkillError(ConfigError):
    """技能文件无法加载。

    继承 `ConfigError` 而不是裸 Exception：技能文件写错**就是**配置错，
    这样 Web 壳层已有的异常处理器会把它转成 400，而不是漏成 500 + 堆栈。
    （与 `DbtMetricsError` 同一个决定。）
    """


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """把 `SKILL.md` 拆成 `(frontmatter, 正文)`。

    只认文件**开头**的 `---` 块。没有 frontmatter 时抛错——
    生态里 `name` 与 `description` 是必填，而且 `description` 是**选择依据**，
    没有它这份技能永远不会被选中，加载它只是浪费。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise SkillError(
            "缺少 YAML frontmatter（文件必须以 --- 开头，"
            "且包含 name 与 description）"
        )
    raw_text = m.group(1)
    body = text[m.end():]

    try:
        import yaml
    except ImportError as e:  # pragma: no cover - 环境缺依赖
        raise SkillError(
            "解析技能 frontmatter 需要 PyYAML：pip install pyyaml"
        ) from e

    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"frontmatter YAML 解析失败: {e}") from e
    if not isinstance(data, dict):
        raise SkillError(
            f"frontmatter 必须是映射，得到 {type(data).__name__}"
        )
    # 把总长也带出去，供上限校验（用原始文本长度，不是解析后的）
    data["__frontmatter_chars__"] = len(raw_text)
    return data, body


@dataclass
class SkillLoadReport:
    """加载报告。**把"哪些技能生效了、哪些没生效、为什么"变成可核对的事实。**"""

    roots: list[str] = field(default_factory=list)
    loaded: list[str] = field(default_factory=list)
    # 磁盘上存在但**没加载**的（外部技能未点名、enabled=false、scope 不匹配）
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # 解析/校验失败
    rejected: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    external_roots: list[str] = field(default_factory=list)
    # P0.5：外置 Prompt 的加载结果（原先静默回退，现在说出来）
    prompt_issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected

    def describe(self) -> dict[str, Any]:
        return {
            "roots": list(self.roots),
            "external_roots": list(self.external_roots),
            "loaded": list(self.loaded),
            "skipped": [{"name": n, "reason": r} for n, r in self.skipped],
            "rejected": [{"name": n, "reason": r} for n, r in self.rejected],
            "warnings": list(self.warnings),
            "prompt_issues": list(self.prompt_issues),
        }

    def lines(self) -> list[str]:
        """给人看的报告行（CLI 与文档共用，避免文案漂移）。"""
        out = [
            f"技能根目录: {', '.join(self.roots) or '（无）'}",
            f"已加载 {len(self.loaded)}: {', '.join(self.loaded) or '（无）'}",
        ]
        if self.external_roots:
            out.append(
                f"外部根（默认不加载）: {', '.join(self.external_roots)}"
            )
        if self.skipped:
            out.append(f"未加载 {len(self.skipped)}:")
            out.extend(f"  - {n}：{r}" for n, r in self.skipped)
        if self.rejected:
            out.append(f"加载失败 {len(self.rejected)}:")
            out.extend(f"  ✗ {n}：{r}" for n, r in self.rejected)
        out.extend(f"  ⚠ {w}" for w in self.warnings)
        out.extend(f"  ⚠ 外置 Prompt: {w}" for w in self.prompt_issues)
        return out


@dataclass
class LoadedSkillCatalog:
    """`SkillCatalog` 的实现。装配期构造，运行期只读。"""

    _items: tuple[SkillSpec, ...] = ()

    def names(self) -> list[str]:
        return [s.name for s in self._items]

    def get(self, name: str) -> SkillSpec | None:
        for s in self._items:
            if s.name == name:
                return s
        return None

    def all(self) -> list[SkillSpec]:
        return list(self._items)

    def for_source(self, source_id: str | None) -> list[SkillSpec]:
        return [s for s in self._items if s.matches_scope(source_id)]

    def catalog_text(self, *, source_id: str | None = None) -> str:
        """渲染成给模型看的目录：**只有 name + description，不含正文**。

        这是渐进披露的那一层：目录常驻（小），正文按需加载（大）。
        """
        items = self.for_source(source_id)
        if not items:
            return ""
        return "\n".join(s.catalog_line() for s in items)


class SkillLoader:
    """扫描并加载技能。

    加载失败**不阻断启动**——一个坏技能不该让整个工具起不来（与插件同理），
    但每一条失败都会进 `report.rejected`，并透出到管理界面。
    """

    def __init__(self, *, max_body_chars: int = DEFAULT_MAX_BODY_CHARS) -> None:
        self._max_body = max_body_chars
        self.report = SkillLoadReport()

    # ------------------------------------------------------------ 入口
    def load(
        self,
        *,
        project_dirs: Iterable[str] = (),
        external_dirs: Iterable[str] = (),
        enabled_external: Iterable[str] = (),
        source_id: str | None = None,
    ) -> LoadedSkillCatalog:
        """加载技能，返回只读目录。

        Args:
            project_dirs: 项目技能目录（**默认加载**）。可以是目录本身，
                也可以直接指到某个 `skills/` 下。
            external_dirs: 外部技能根（默认**不加载**，除非被 `enabled_external` 点名）。
            enabled_external: 外部技能白名单。空 = 一个都不加载。
                写 `"*"` 表示全部加载。
            source_id: 当前数据源，用于 `scope` 过滤。
        """
        self.report = SkillLoadReport()
        allow_all = "*" in set(enabled_external)
        allowed = {n for n in enabled_external if n != "*"}

        found: dict[str, SkillSpec] = {}

        # ---- 1) 项目技能：默认加载 ----
        for root in project_dirs:
            self.report.roots.append(root)
            for spec in self._scan(root, origin="project"):
                self._admit(spec, found, source_id=source_id,
                            trusted=True, allow_all=allow_all, allowed=allowed)

        # ---- 2) 外部技能：默认不加载 ----
        for root in external_dirs:
            expanded = os.path.expanduser(root)
            self.report.external_roots.append(root)
            if not os.path.isdir(expanded):
                continue
            for spec in self._scan(expanded, origin="external"):
                self._admit(spec, found, source_id=source_id,
                            trusted=False, allow_all=allow_all, allowed=allowed)

        items = tuple(found[n] for n in sorted(found))
        self.report.loaded = sorted(found)
        return LoadedSkillCatalog(_items=items)

    # ------------------------------------------------------------ 准入
    def _admit(self, spec: SkillSpec, found: dict[str, SkillSpec], *,
               source_id: str | None, trusted: bool, allow_all: bool,
               allowed: set[str]) -> None:
        """判断一份技能该不该进目录。**每个"不加载"都要给出理由。**"""
        key = spec.name

        # 项目技能不允许被外部同名技能覆盖——那是最坏的一种"静默替换"
        existing = found.get(key)
        if existing is not None:
            if existing.origin == "project" and spec.origin != "project":
                self.report.skipped.append(
                    (key, f"已被项目技能覆盖（{existing.path}）")
                )
                return
            self.report.warnings.append(
                f"技能 {key} 重复定义，后一个覆盖前一个: "
                f"{existing.path} → {spec.path}"
            )

        if not spec.enabled:
            self.report.skipped.append((key, "文件里 enabled = false"))
            return
        if not spec.matches_scope(source_id):
            self.report.skipped.append(
                (key, f"scope={spec.scope} 与当前数据源 {source_id or '（未指定）'} 不符")
            )
            return
        if not trusted and not (allow_all or key in allowed):
            self.report.skipped.append(
                (key, "外部技能，未在 [skills].enabled 中点名（默认不加载）")
            )
            return
        found[key] = spec

    # ------------------------------------------------------------ 扫描
    def _scan(self, root: str, *, origin: str) -> list[SkillSpec]:
        """扫一个根目录下的技能。

        支持两种形状（生态两种都有）：
            <root>/<name>/SKILL.md     目录式打包（可带 references/ scripts/）
            <root>/<name>.md           平铺单文件
        """
        base = Path(root)
        if not base.is_dir():
            return []
        out: list[SkillSpec] = []

        for child in sorted(base.iterdir()):
            if child.name.startswith((".", "_")):
                continue
            if child.is_dir():
                md = child / "SKILL.md"
                if md.is_file():
                    # 目录里的 SKILL.md：**文件名已经表明了意图**，
                    # 所以缺 frontmatter 是错误，要报出来
                    spec = self._parse_file(md, origin=origin,
                                            fallback_name=child.name,
                                            strict=True)
                    if spec is not None:
                        out.append(spec)
            elif child.suffix.lower() == ".md" and child.is_file():
                # 平铺 .md：根目录里放 README.md 是很自然的事，
                # 不能把它当成"写坏了的技能"去报错
                spec = self._parse_file(child, origin=origin,
                                        fallback_name=child.stem,
                                        strict=child.name.upper() == "SKILL.MD")
                if spec is not None:
                    out.append(spec)
        return out

    def _parse_file(self, path: Path, *, origin: str, fallback_name: str,
                    strict: bool) -> SkillSpec | None:
        """解析单个文件。失败记进 report，返回 None（**不抛**，不阻断别的技能）。

        `strict=False` 时，**缺 frontmatter** 降级为"跳过"而不是"失败"——
        判据是"文件名有没有表明它是技能"（`SKILL.md` 表明，`README.md` 没有）。
        这条区分是有价值的：否则每个技能根目录里放一份说明文档都会变成
        一条红色的加载失败，用户很快就会学会忽略这个报告。
        """
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            self.report.rejected.append((fallback_name, f"读取失败: {e}"))
            return None

        try:
            spec = self._build(text, path=path, origin=origin,
                               fallback_name=fallback_name)
        except SkillError as e:
            # 报错时用**声明的名字**而不是目录名——用户认的是 frontmatter 里的
            # `name`，报告里出现目录名会让他找不着是哪一份。
            label = _declared_name(text) or fallback_name
            if not strict and "frontmatter" in str(e):
                self.report.skipped.append(
                    (label, f"没有 frontmatter，按普通文档忽略（{path.name}）")
                )
                return None
            self.report.rejected.append((label, str(e)))
            return None
        for w in spec.warnings:
            self.report.warnings.append(f"{spec.name}: {w}")
        return spec

    def _build(self, text: str, *, path: Path, origin: str,
               fallback_name: str) -> SkillSpec:
        fm, body = split_frontmatter(text)

        chars = int(fm.pop("__frontmatter_chars__", 0))
        if chars > FRONTMATTER_MAX_CHARS:
            raise SkillError(
                f"frontmatter 过长（{chars} 字符 > 上限 "
                f"{FRONTMATTER_MAX_CHARS}）——部分 agent 会直接拒收这份技能"
            )

        warnings: list[str] = []
        name = str(fm.get("name") or "").strip()
        if not name:
            raise SkillError("frontmatter 缺少 name")
        if not _NAME_RE.match(name):
            raise SkillError(
                f"技能名不合法: {name!r}（只能用字母数字、连字符、下划线和点）"
            )
        if name != fallback_name and not str(fm.get("name")):
            name = fallback_name

        description = str(fm.get("description") or "").strip()
        if not description:
            # 生态必填，而且它是**选择依据**：没有它就永远不会被选中
            raise SkillError(
                "frontmatter 缺少 description——它是模型判断"
                "「这个技能该不该用」的唯一依据，不能省"
            )
        if len(description) > DESCRIPTION_MAX_CHARS:
            warnings.append(
                f"description 偏长（{len(description)} 字符），"
                f"建议控制在 {DESCRIPTION_MAX_CHARS} 以内以免稀释匹配"
            )

        ns = fm.get("nl2sql") or {}
        if not isinstance(ns, dict):
            raise SkillError("nl2sql: 必须是映射")
        unknown = sorted(str(k) for k in ns if k not in _KNOWN_NS_KEYS)
        if unknown:
            warnings.append(f"nl2sql: 下有不认识的键被忽略 {unknown}")

        kind = str(ns.get("kind") or "playbook").strip()
        if kind not in VALID_KINDS:
            raise SkillError(
                f"nl2sql.kind 不合法: {kind!r}；可用: {', '.join(VALID_KINDS)}"
            )

        requires = _as_str_tuple(ns.get("requires"))
        if requires:
            from ..contracts.tool import CAPABILITIES
            bad = [r for r in requires if r not in CAPABILITIES]
            if bad:
                raise SkillError(
                    f"nl2sql.requires 里有未知能力 {bad}；"
                    f"可用: {', '.join(sorted(CAPABILITIES))}"
                )

        body = body.strip()
        if not body:
            raise SkillError("正文为空——技能的价值全在正文里")
        if len(body) > self._max_body:
            # **拒绝而不是截断**：截断一份流程等于毁掉它
            raise SkillError(
                f"正文过长（{len(body)} 字符 > 上限 {self._max_body}）"
                f"——超限会被拒绝而不是截断（截断的流程不成立）。"
                f"把重型参考拆到同目录的附属文件里，正文只留步骤"
            )

        scope_raw = ns.get("scope")
        scope = None if scope_raw in (None, "") else str(scope_raw)

        return SkillSpec(
            name=name,
            description=description,
            kind=kind,
            requires=requires,
            scope=scope,
            enabled=bool(ns.get("enabled", True)),
            applies_to=_as_str_tuple(ns.get("applies_to")),
            body=body,
            origin=origin,
            path=str(path),
            warnings=tuple(warnings),
        )


# `nl2sql:` 命名空间下认识的键。**不认识的键发警告而不是报错**——
# 生态在演进，硬拒绝会让新版本的文件加载不了；但也不能不说。
_KNOWN_NS_KEYS = frozenset({
    "kind", "requires", "scope", "enabled", "applies_to",
})


def _declared_name(text: str) -> str:
    """尽力从 frontmatter 里取出 `name`（**不校验**，只为把报告写得可认）。

    解析失败时用它当报告的标签：用户认的是 frontmatter 里的名字，
    报告里出现目录名会让他不知道是哪一份出了问题。
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return ""
    for line in m.group(1).splitlines():
        if line.lstrip().startswith("name:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def _as_str_tuple(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    if isinstance(v, (list, tuple)):
        return tuple(str(x) for x in v)
    return (str(v),)
