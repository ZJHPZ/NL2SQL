"""提交前机密扫描：只看暂存区，不看工作区（工作区可能有被忽略的文件）。

规则刻意只有四条 —— 扫描器一旦开始报假警，很快就没人看了，
然后真警也被无视。所以占位符（`"..."`、`"<口令>"`、`"xxx"`）一律跳过：
文档里写示例是正常行为，不该被当成泄密。

用法：
    python scripts/scan_secrets.py      # 退出码 0=干净 1=有命中
"""
import re
import subprocess
import sys

# 有捕获组 v 的规则会把值取出来，供占位符判断
PATTERNS = [
    ("Key 字符串", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("api_key 赋值", re.compile(r"api[_-]?key\s*[:=]\s*['\"](?P<v>[^'\"]{8,})['\"]", re.I)),
    ("password 赋值", re.compile(r"password\s*[:=]\s*['\"](?P<v>[^'\"]{3,})['\"]", re.I)),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# 占位符：全是标点 / 全是 x 或 * / 尖括号包起来 / 明显是变量名
PLACEHOLDER = re.compile(r"^[.\-_*·xX\s]*$|^<[^>]*>$|^\$\{[^}]*\}$|^%\([^)]*\)s$")


def is_placeholder(value: str) -> bool:
    return bool(PLACEHOLDER.match(value.strip()))


def staged_files() -> list[str]:
    """`-z` + 不转义路径：中文文件名不会被 git 变成 C 风格转义。"""
    raw = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--cached", "--name-only",
         "-z", "--diff-filter=ACMR"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8")
    return [p for p in raw.split("\0") if p]


def main() -> int:
    files = staged_files()
    hits = 0

    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    for label, pattern in PATTERNS:
                        match = pattern.search(line)
                        if not match:
                            continue
                        value = match.groupdict().get("v")
                        if value and is_placeholder(value):
                            continue
                        hits += 1
                        print(f"[{label}] {path}:{lineno}: {line.strip()[:110]}")
        except OSError as exc:
            print(f"[跳过] {path}: {exc}")

    blocked = [
        p for p in files
        if p.startswith("data/") or p.endswith(".env") or "/.env" in p or "local.toml" in p
    ]
    for path in blocked:
        hits += 1
        print(f"[不该入库] {path}")

    print(f"\n暂存 {len(files)} 个文件，命中 {hits} 处")
    if hits:
        print("结论：先处理上面的命中项（占位符已自动跳过，报出来的都是真东西）")
        return 1
    print("结论：可以提交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
