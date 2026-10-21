"""提交前检查（钩子和手动调用共用一份实现）。

为什么抽成 Python 而不是写在钩子里：钩子里写逻辑，只能在 `git commit` 时跑，
排查问题时没法单独跑。抽出来以后两种用法等价：

    python scripts/precommit.py     # 手动跑
    git commit ...                  # 由 scripts/hooks/pre-commit 调用

检查项只有两条，都是「事后发现代价很高」的：
    1. 层约束 —— contracts/ 只许标准库；nl2sql/ 不许 import 壳或 Web 框架
    2. 机密扫描 —— 暂存内容里不许出现 Key、密码、data/ 与 .env

退出码：0 通过，1 检查未通过（钩子据此中止提交）。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CHECKS = [
    ("层约束", "scripts/check_layers.py"),
    ("机密扫描", "scripts/scan_secrets.py"),
]


def run_one(label: str, script: str) -> bool:
    print(f"[precommit] {label} ...")
    result = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        print(output)
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        print(f"[precommit] {label} 未通过")
        return False
    # 成功时只打印最后一行（"通过 / 命中 0 处"），不刷屏
    tail = output.splitlines()[-1] if output else "ok"
    print(f"[precommit] {label} 通过 —— {tail}")
    return True


def main() -> int:
    failed = [label for label, script in CHECKS if not run_one(label, script)]
    if failed:
        print(f"\n[precommit] 未通过：{'、'.join(failed)}，提交应中止。")
        return 1
    print("\n[precommit] 全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
