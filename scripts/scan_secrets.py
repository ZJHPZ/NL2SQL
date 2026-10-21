"""提交前机密扫描：只看暂存区，不看工作区（工作区可能有被忽略的文件）。"""
import re
import subprocess

PATTERNS = [
    ("DeepSeek/OpenAI 风格 Key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("api_key 赋值", re.compile(r"api[_-]?key\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]", re.I)),
    ("password 赋值", re.compile(r"password\s*[:=]\s*['\"][^'\"]{3,}['\"]", re.I)),
    ("私钥块", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

files = subprocess.run(
    ["git", "-c", "core.quotepath=false", "diff", "--cached", "--name-only", "-z",
     "--diff-filter=ACMR"],
    capture_output=True, check=True,
).stdout.decode("utf-8").split("\0")
files = [p for p in files if p]

hits = 0
for path in files:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                for label, pat in PATTERNS:
                    if pat.search(line):
                        hits += 1
                        print(f"[{label}] {path}:{lineno}: {line.strip()[:110]}")
    except OSError as exc:
        print(f"[跳过] {path}: {exc}")

blocked = [p for p in files if p.startswith("data/") or ".env" in p or "local.toml" in p]
for path in blocked:
    hits += 1
    print(f"[不该入库] {path}")

print(f"\n暂存 {len(files)} 个文件，命中 {hits} 处")
print("结论：" + ("可以提交" if hits == 0 else "先处理上面的命中项"))
