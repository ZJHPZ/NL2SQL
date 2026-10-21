"""层依赖检查：把架构约束变成可执行的门槛。

检查项
------
1. 核心 `nl2sql/` 不得 import 任何 `apps/*`（反向依赖）
2. 核心 `nl2sql/` 不得 import Web 框架（保证 SDK 抽包干净）
3. `nl2sql/contracts/` 只依赖标准库（允许 contracts 内部互相引用——
   契约层是一个整体，共享数据模型是它的职责）
4. 核心不得出现模块级可变全局状态（连接 / 缓存 / 单例）
5. 三个壳之间互不引用

用法:
    python scripts/check_layers.py          # 检查，违规则退出码 1
"""
from __future__ import annotations

import ast
import io
import os
import sys

# Windows 控制台默认 GBK，中文与符号会编码失败；统一按 UTF-8 输出
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "nl2sql")
APPS = os.path.join(ROOT, "apps")

WEB_FRAMEWORKS = {
    "fastapi", "flask", "django", "uvicorn", "starlette",
    "jinja2", "sanic", "aiohttp", "tornado", "bottle",
}

# 允许在核心中出现的第三方依赖（其余第三方一律报可疑，需显式加白名单）
ALLOWED_THIRD_PARTY = {
    "langgraph", "langchain", "langchain_core", "sqlglot", "sqlalchemy",
    "pydantic", "yaml", "numpy", "pandas",
    # 模型客户端：仅用于 OpenAI 兼容端点（DeepSeek/通义/Kimi/GLM/OpenAI），
    # 且在 llm/providers.py 里延迟导入——没装也能用规则规划器
    "openai",
    # MySQL 驱动：只在 sources/mysql.py 里导入，且是可选依赖
    # （`pip install .[mysql]`）。没装时连不上库，但核心层仍能 import 起来。
    "pymysql",
}

STDLIB = set(sys.stdlib_module_names)

# 这些工厂产出的是**不可变**对象，模块级声明属于常量，不算运行时状态。
#   frozenset/tuple/dict/set/list : 字面量集合
#   re.compile                    : 预编译正则（Pattern 不可变）
#   TypeVar / NewType             : 类型声明
_IMMUTABLE_FACTORIES = {
    "frozenset", "tuple", "dict", "set", "list",
    "compile", "TypeVar", "NewType", "namedtuple",
}


class Violation:
    def __init__(self, path: str, line: int, rule: str, detail: str) -> None:
        self.path, self.line, self.rule, self.detail = path, line, rule, detail

    def __str__(self) -> str:
        rel = os.path.relpath(self.path, ROOT)
        return f"  [{self.rule}] {rel}:{self.line}\n      {self.detail}"


def iter_py(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"__pycache__", ".venv"}]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def is_dunder_all(node: ast.stmt) -> bool:
    """判断是否为 __all__ 赋值（允许的模块级列表）。"""
    targets = getattr(node, "targets", [])
    return any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)


def top_level_imports(tree: ast.Module):
    """只收集模块级（含 TYPE_CHECKING 内）的导入。"""
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node


def _is_constant_value(value: ast.expr) -> bool:
    """常量字面量集合不算"可变全局状态"。

    架构约束针对的是**连接 / 缓存 / 单例**这类运行时状态。
    模块级 `frozenset({...})`、`{...}` 字面量常量表是正常写法，
    把它们也拦掉只会逼出"为了过检查而把常量塞进函数"的坏代码。
    """
    if isinstance(value, ast.Call):
        func = value.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        return name in _IMMUTABLE_FACTORIES
    return isinstance(value, (ast.Dict, ast.List, ast.Set, ast.Tuple))


def check_core() -> list[Violation]:
    out: list[Violation] = []
    contracts_dir = os.path.join(CORE, "contracts")

    for path in iter_py(CORE):
        rel = os.path.relpath(path, ROOT)
        in_contracts = os.path.abspath(path).startswith(os.path.abspath(contracts_dir))
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        except SyntaxError as e:
            out.append(Violation(path, e.lineno or 0, "SYNTAX", str(e)))
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    root = a.name.split(".")[0]
                    out += _judge(path, rel, in_contracts, root, a.name, node.lineno)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    # 相对导入：还原到顶层模块名
                    pkg_parts = rel.split(os.sep)[:-1]
                    base = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level > 1 else pkg_parts
                    root = base[0] if base else ""
                    out += _judge(path, rel, in_contracts, root, node.module or root, node.lineno)
                elif node.module:
                    root = node.module.split(".")[0]
                    out += _judge(path, rel, in_contracts, root, node.module, node.lineno)

        # 模块级可变全局（常量字面量除外）
        for stmt in tree.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                if is_dunder_all(stmt):
                    continue
                if _is_constant_value(stmt.value):
                    continue
                if isinstance(stmt.value, ast.Call):
                    # 形如 cache = LRUCache() / conn = connect(...) 之类需要人工确认
                    name = getattr(stmt.value.func, "id", None) or getattr(
                        stmt.value.func, "attr", None
                    )
                    if name in _IMMUTABLE_FACTORIES:
                        continue
                    out.append(Violation(
                        path, stmt.lineno, "GLOBAL_STATE",
                        f"模块级调用初始化（疑似运行时状态）: {name}()",
                    ))
    return out


def _judge(path, rel, in_contracts, root, full, lineno) -> list[Violation]:
    out: list[Violation] = []
    if root == "apps":
        out.append(Violation(path, lineno, "REVERSE_DEP",
                            f"核心反向依赖壳层: {full}"))
    if root in WEB_FRAMEWORKS:
        out.append(Violation(path, lineno, "WEB_IN_CORE",
                            f"核心不得依赖 Web 框架: {full}（会污染 SDK 抽包）"))
    if in_contracts and root not in STDLIB and root not in {"__future__", "nl2sql"}:
        out.append(Violation(path, lineno, "CONTRACT_DEP",
                            f"契约层只允许标准库，发现第三方依赖: {full}"))
    # 契约层内部的相对导入（..contracts.x / .y）是允许的：
    # 契约是一个整体，共享数据模型是它的职责，不算"依赖核心实现"。
    if root not in STDLIB and root not in ALLOWED_THIRD_PARTY and root not in {
        "nl2sql", "apps", "__future__", "scripts", "_stubs"
    }:
        out.append(Violation(path, lineno, "UNKNOWN_DEP",
                            f"未在白名单的第三方依赖: {full}（确需则加入 ALLOWED_THIRD_PARTY）"))
    return out


def check_apps() -> list[Violation]:
    out: list[Violation] = []
    if not os.path.isdir(APPS):
        return out
    shells = [d for d in os.listdir(APPS) if os.path.isdir(os.path.join(APPS, d))]
    for path in iter_py(APPS):
        rel = os.path.relpath(path, APPS)
        own = rel.split(os.sep)[0]
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("apps."):
                        tgt = a.name.split(".")[1]
                        if tgt != own:
                            out.append(Violation(path, node.lineno, "SHELL_COUPLING",
                                                 f"壳之间不得互相引用: {a.name}"))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("apps."):
                    tgt = node.module.split(".")[1]
                    if tgt != own:
                        out.append(Violation(path, node.lineno, "SHELL_COUPLING",
                                             f"壳之间不得互相引用: {node.module}"))
    return out


# 插件唯一被允许依赖的核心模块。
# **这是插件沙箱的落地方式**：若能 import 核心内部任意模块，
# 插件就能绕过能力检查直接拿适配器，白名单也就形同虚设。
PLUGIN_API = "nl2sql.plugins"


def check_plugins() -> list[Violation]:
    """插件只能依赖 `nl2sql.plugins`（稳定接口），不得碰核心内部。"""
    out: list[Violation] = []
    root = os.path.join(ROOT, "plugins")
    if not os.path.isdir(root):
        return out
    for path in iter_py(root):
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if not name.startswith("nl2sql"):
                    continue
                if name == PLUGIN_API or name.startswith(PLUGIN_API + "."):
                    continue
                out.append(Violation(
                    path, node.lineno, "PLUGIN_IMPORT",
                    f"插件只能 import {PLUGIN_API}，不得依赖核心内部: {name}",
                ))
    return out


def main() -> int:
    violations = check_core() + check_apps() + check_plugins()
    n_core = sum(1 for _ in iter_py(CORE))
    n_plugins = sum(1 for _ in iter_py(os.path.join(ROOT, "plugins"))) \
        if os.path.isdir(os.path.join(ROOT, "plugins")) else 0
    print(f"检查范围: nl2sql/（{n_core} 个文件）+ apps/ + plugins/（{n_plugins} 个文件）")
    if not violations:
        print("层依赖检查通过 [OK]")
        return 0
    print(f"\n发现 {len(violations)} 处违规:\n")
    by_rule: dict[str, list[Violation]] = {}
    for v in violations:
        by_rule.setdefault(v.rule, []).append(v)
    for rule, items in sorted(by_rule.items()):
        print(f"{rule}（{len(items)}）")
        for v in items:
            print(v)
        print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
