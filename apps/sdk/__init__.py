"""对外 SDK 壳。

定位：让别人在自己的项目里三行代码用起来。
纪律：**这一层几乎没有代码**——只做再导出与便捷构造。

用法（S7 起）::

    from apps.sdk import NL2SQL, NL2SQLConfig

    cfg = NL2SQLConfig().add_sqlite("mydata.db")
    out = NL2SQL(cfg).ask("上个月各渠道的销售额")

设计要点：这里**不得**出现任何业务逻辑。一旦在此写逻辑，SDK 就会与 Web 行为分叉，
"一个核心 + 三个壳"的架构承诺随之失效。
"""
