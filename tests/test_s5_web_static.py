"""S5 验收（静态托管 + 真实启动）：验证前端产物确实被服务出去。

与 `test_s5_web_api.py` 的分工：
    那个测**接口契约**（用 TestClient，快）
    这个测**托管与真实进程**（起真的 uvicorn，慢但更接近用户实际路径）

覆盖：
    1. FastAPI 能托管 Vite 构建产物（`/`、`/assets/*`）
    2. SPA 回退正确，且 `/api/*` 拼错时返回 JSON 404 而不是 HTML——
       否则前端会把 API 错误当成页面，排障时非常迷惑
    3. 真实 uvicorn 进程能起来并响应（TestClient 不走网络，覆盖不到这层）

运行:
    python tests/test_s5_web_static.py

说明：**前端交互（点排序、改 SQL 重跑、点候选字段）无法自动验证**，
需要人在浏览器里点。这里只保证"页面能被正确送出、资源路径对"。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN_DB = os.path.join(ROOT, "golden_set", "ecommerce.db")
DIST = os.path.join(ROOT, "apps", "web", "frontend", "dist")

_failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        _failures.append(f"{label} {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str, dict]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def http_post(url: str, payload: dict, timeout: float = 60.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    print("[1] 构建产物存在性")
    if not os.path.isdir(DIST):
        check(False, "前端已构建（dist 存在）",
              f"缺少 {DIST}；请先 cd apps/web/frontend && npm run build")
        return 1
    check(True, "前端已构建（dist 存在）")

    index = os.path.join(DIST, "index.html")
    check(os.path.isfile(index), "dist/index.html 存在")
    html = open(index, encoding="utf-8").read()
    check('id="root"' in html, "index.html 含挂载点 #root")
    check("/assets/" in html, "index.html 引用 /assets/ 资源",
          html[:200])

    assets_dir = os.path.join(DIST, "assets")
    assets = os.listdir(assets_dir) if os.path.isdir(assets_dir) else []
    check(any(a.endswith(".js") for a in assets), "有 JS 产物", str(assets))
    check(any(a.endswith(".css") for a in assets), "有 CSS 产物", str(assets))

    # ---------------------------------------------------------- 真实进程
    base = os.environ.get("NL2SQL_TEST_TMP") or os.path.join(ROOT, "data", "_test_s5s")
    work = os.path.join(base, "run")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    db = os.path.join(work, "shop.db")
    shutil.copy2(GOLDEN_DB, db)

    port = free_port()
    env = dict(os.environ)
    env.update({
        "NL2SQL_DB": db,
        "NL2SQL_STATE_DIR": os.path.join(work, "state").replace("\\", "/"),
        "NL2SQL_SOURCE_ID": "shop",
        "NL2SQL_PLANNER": "rule",
    })
    env.pop("NL2SQL_LLM_API_KEY", None)

    print(f"\n[2] 真实 uvicorn 进程（端口 {port}）")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "apps.web.backend.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        # 等就绪
        ready = False
        for _ in range(60):
            try:
                code, _, _ = http_get(f"{base_url}/api/health", timeout=2)
                if code == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        check(ready, "服务在 30s 内就绪")
        if not ready:
            proc.kill()
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            print(out[-1500:])
            return 1

        # 静态资源
        code, body, headers = http_get(f"{base_url}/")
        check(code == 200, "GET / 返回 200", str(code))
        check('id="root"' in body, "GET / 返回前端页面")
        check("text/html" in headers.get("content-type", ""),
              "Content-Type 是 html", headers.get("content-type", ""))

        js = next((a for a in assets if a.endswith(".js")), None)
        if js:
            code, body, headers = http_get(f"{base_url}/assets/{js}")
            check(code == 200, f"GET /assets/{js} 返回 200", str(code))
            check(len(body) > 1000, "JS 资源有内容", f"{len(body)} 字节")

        css = next((a for a in assets if a.endswith(".css")), None)
        if css:
            code, body, _ = http_get(f"{base_url}/assets/{css}")
            check(code == 200 and len(body) > 500, f"GET /assets/{css} 可访问",
                  str(code))

        # SPA 回退
        code, body, _ = http_get(f"{base_url}/some/deep/route")
        check(code == 200 and 'id="root"' in body, "SPA 回退返回 index.html")

        # /api 拼错必须是 JSON 404，不能回退成 HTML
        code, body, headers = http_get(f"{base_url}/api/typo/here")
        check(code == 404, "/api 未知路径返回 404（不回退成 HTML）", str(code))
        check("json" in headers.get("content-type", ""),
              "/api 404 是 JSON", headers.get("content-type", ""))
        try:
            parsed = json.loads(body)
            check(parsed.get("error") == "not_found", "404 错误体结构化",
                  str(parsed))
        except Exception:  # noqa: BLE001
            check(False, "404 错误体可解析", body[:120])

        # 经过真实网络的问答
        print("\n[3] 真实网络下的问答与安全边界")
        code, body = http_post(
            f"{base_url}/api/query",
            {"question": "2024年6月，数码品类的实收金额是多少？",
             "now": "2024-06-30"})
        check(code == 200, "POST /api/query 200", str(code))
        d = json.loads(body)
        check(d["status"] == "ok", "status=ok", f"{d['status']} {d.get('message')}")
        check(d["result"]["rows"][0][0] == 99998.5, "结果值正确",
              str(d["result"]["rows"]))

        code, body = http_post(f"{base_url}/api/execute",
                               {"sql": "DROP TABLE fact_order"})
        d = json.loads(body)
        check(code == 200 and d["status"] == "blocked",
              "★ 真实网络下写操作仍被 guard 拒绝",
              f"{code} {d.get('status')}")

        code, _body, _hdrs = http_get(f"{base_url}/docs")
        check(code == 200, "OpenAPI 文档可访问", str(code))

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(work, ignore_errors=True)

    print("\n" + "=" * 64)
    if _failures:
        print(f"S5 静态托管验收未通过：{len(_failures)} 项失败")
        for x in _failures[:20]:
            print("  -", x)
        return 1
    print("S5 静态托管验收全部通过")
    print("\n注意：以下需要人工在浏览器验证（无法自动测）：")
    print("  · 结果表点表头排序、过滤框、导出 CSV")
    print("  · SQL 编辑后 Ctrl+Enter 重跑")
    print("  · 候选字段点击复制、知识库测试检索")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
