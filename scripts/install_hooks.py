"""把仓库自带的 git 钩子接上（只改本仓库的 core.hooksPath）。

为什么需要这个脚本：`.git/hooks/` 里的东西不入库，换台机器就没了。
把钩子源码放在 `scripts/hooks/` 里入库，再用 `core.hooksPath` 指过去，
钩子才跟着代码一起走。

用法：
    python scripts/install_hooks.py          # 装上
    python scripts/install_hooks.py --off    # 卸掉
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = "scripts/hooks"


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{result.stderr.strip()}")
    return result.stdout.strip()


def main(argv: list[str]) -> int:
    try:
        _git("rev-parse", "--git-dir")
    except SystemExit as exc:
        print(f"这里不是 git 仓库，先 git init。（{exc}）")
        return 1

    if "--off" in argv:
        _git("config", "--unset", "core.hooksPath")
        print("已卸掉 core.hooksPath，git 回到默认的 .git/hooks。")
        return 0

    if not (ROOT / HOOKS / "pre-commit").exists():
        print(f"缺少 {HOOKS}/pre-commit，无法安装。")
        return 1

    _git("config", "core.hooksPath", HOOKS)
    print(f"core.hooksPath = {HOOKS}")
    print("钩子从下次提交开始生效：层约束 + 机密扫描。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
