"""检查三处版本号是否一致（单一来源 = `nl2sql/__init__.py`）。

为什么需要有这个脚本，而不是"大家记得一起改"：

    版本号散在三个地方，其中只有一个能当权威：
      * `nl2sql/__init__.py:__version__`  —— 权威。运行时 `nl2sql.__version__` 读它。
      * `pyproject.toml`                  —— 已改成 `dynamic`，构建时从权威处读。
      * `apps/web/frontend/package.json`  —— npm 强制要一个 version 字面量，读不了 Python。

所以真正需要人工同步的只有 package.json 一处。这个脚本就是那个提醒：
发布前端之前跑一次，不一致就红。

用法：
    python scripts/check_version.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "nl2sql" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
PACKAGE = ROOT / "apps" / "web" / "frontend" / "package.json"


def authoritative_version() -> str:
    text = INIT.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise SystemExit(f"{INIT} 里找不到 __version__")
    return match.group(1)


def package_json_version() -> str:
    if not PACKAGE.exists():
        return ""
    return str(json.loads(PACKAGE.read_text(encoding="utf-8")).get("version", ""))


def pyproject_is_dynamic() -> bool:
    """pyproject 是否已交给 dynamic 读，而不是又手写一份。"""
    text = PYPROJECT.read_text(encoding="utf-8")
    head = text.split("[project.optional-dependencies]")[0]
    return bool(re.search(r'^dynamic\s*=\s*\[.*"version"', head, re.M))


def main() -> int:
    want = authoritative_version()
    problems: list[str] = []

    if not pyproject_is_dynamic():
        problems.append(
            "pyproject.toml 的 [project] 又手写 version 了。"
            "应保持 dynamic = [\"version\"]，由 nl2sql.__version__ 提供。"
        )

    got = package_json_version()
    if got != want:
        problems.append(
            f"apps/web/frontend/package.json 是 {got or '（缺失）'}，"
            f"权威版本是 {want}。改 package.json 的 version 字段。"
        )

    if problems:
        print(f"版本号不一致（权威 = {want}）：")
        for item in problems:
            print(f"  - {item}")
        return 1

    print(f"版本号一致：{want}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
