"""本地 Web 壳（首发形态）。

定位：把核心引擎包成 HTTP 服务 + 前端页面。
纪律：**不写业务逻辑**，只做参数装配与结果渲染。

启动（S5 起可用）::

    uvicorn apps.web.backend.main:app --reload --port 8000

依赖核心的方式只有一种：构造 `NL2SQL(config)` 实例并调用 `ask()`。
不得 import `nl2sql` 的内部私有模块。
"""
