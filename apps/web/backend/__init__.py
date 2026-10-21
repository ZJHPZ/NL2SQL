"""本地 Web 壳后端（FastAPI）。

启动：
    uvicorn apps.web.backend.main:app --reload --port 8000

纪律：**不写业务逻辑**。这里只做参数装配、结果序列化、HTTP 语义。
一切查询行为来自 `nl2sql` 核心，壳不重复实现也不绕过。
"""
