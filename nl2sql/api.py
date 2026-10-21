"""对外门面：`NL2SQL`。

这是三个壳（Web / SDK / 内部工具）唯一需要接触的类。

设计要点
--------
1. **构造即装配**：依赖在 `__init__` 里一次性建好并持有，运行时不再查全局。
2. **无模块级状态**：同一个进程里可以有多个 `NL2SQL` 实例互不干扰
   （多数据源、测试、内部工具的多个工作区都靠这个）。
3. **门面只做编排，不做业务**：真正的逻辑在各模块里。
"""
from __future__ import annotations

import os
from typing import Any

from . import __version__
from .config import NL2SQLConfig
from .contracts.knowledge import KnowledgeEntry, RetrievalHit
from .contracts.pipeline import QueryOutcome
from .contracts.source import QueryResult, TableMeta
from .contracts.storage import StorageBundle
from .errors import ConfigError
from .registry import Registry


class NL2SQL:
    """自然语言查询数据仓的核心门面。

    用法::

        cfg = NL2SQLConfig(state_dir="data").add_sqlite("golden_set/ecommerce.db")
        engine = NL2SQL(cfg)
        out = engine.ask("2024年6月，数码品类的实收金额是多少？")
        if out.ok:
            print(out.sql)
            print(out.result.columns, out.result.rows)

    一期只实现 `ask()`；`history` / `sources` 等只读能力随 S2/S6 补齐。
    """

    def __init__(self, config: NL2SQLConfig, *, registry: Registry | None = None,
                 semantic_model: Any = None, llm_provider: Any = None) -> None:
        config.validate()
        self.config = config
        # 每个实例自己的注册表：先装入内置实现，再允许外部覆盖
        self.registry = registry or Registry()
        self._register_builtins()

        # 语义模型与模型提供者：显式注入，不读全局、不读环境变量
        # 加载报告（若语义模型来自 dbt metrics.yml）：可移植性 / 被拒绝的指标
        self.semantic_report: Any = None
        self.semantic_model = semantic_model or self._default_semantic_model()
        self.llm_provider = llm_provider
        # 实际生效的规划器模式（可能因缺少模型而降级），供壳层展示
        self.planner_mode: str = "未装配"

        # 存储装配
        self.storage: StorageBundle | None = None
        # 运行时组件（按数据源惰性装配，缓存在实例上而非模块上）
        self._adapters: dict[str, Any] = {}
        self._catalog: Any = None
        self._knowledge: Any = None
        self._cache: Any = None
        self._agent: Any = None
        # agent 模式组件
        self._runtime: Any = None
        self._tools: Any = None
        self._tool_deps: Any = None
        self._guard: Any = None
        self._validator: Any = None
        self._pipeline_planner: Any = None
        self.tools: Any = None
        self.tool_skipped: list[str] = []
        self.plugin_errors: list[str] = []
        # 技能目录与报告（装配期一次加载，运行期只读）
        self.skill_catalog: Any = None
        self.skill_report: Any = None

    def _default_semantic_model(self) -> Any:
        """装配默认语义模型。**优先用 dbt metrics.yml**。

        优先级：
            1. 数据源配置里的 `semantic_path`（dbt 指标文件）—— 显式指定优先
            2. 内置黄金集模型（电商数仓）—— 评测与演示用

        为什么内置模型还留着：黄金集是评测基准，30 题基线、缓存指纹、
        同义词表都建立在它上面。它现在**不再是有语义层本体**，而是一份
        "参考实现"——`golden_set/metrics.yml` 用 dbt 语法重述了它，
        并由 `tests/test_s3b_dbt_loader.py` 断言两者行为等价。
        """
        path = None
        source_id = "shop"
        try:
            cfg = self.config.resolve_source()
            path = cfg.semantic_path
            source_id = cfg.source_id
        except Exception:  # noqa: BLE001 - 配置尚未就绪时走内置模型
            path = None

        if path:
            from .semantics.dbt_loader import DbtMetricsLoader

            loader = DbtMetricsLoader(source_id=source_id)
            model = loader.load(path)
            self.semantic_report = loader.report
            return model

        from .semantics.golden_model import build_model

        return build_model(order_level=True)

    # ------------------------------------------------------------ 装配
    def connect(self) -> "NL2SQL":
        """装配存储、适配器与元数据目录。**幂等**。

        刻意做成本地显式调用而非 `__init__` 里自动执行：
        `__init__` 不该有副作用（连数据库/写文件），否则构造一个实例就会失败。
        """
        if self.storage is not None:
            return self

        from .catalog import CatalogService
        from .sources import build_adapter
        from .storage import build_storage
        from .storage.sqlite_meta import source_config_to_storage

        self.storage = build_storage(self.config)

        # 把数据源配置落盘，便于 Web 壳重启后恢复
        for cfg in self.config.sources.values():
            self.storage.meta.save_source(source_config_to_storage(cfg))

        for cfg in self.config.sources.values():
            self._adapters[cfg.source_id] = build_adapter(
                cfg, registry=self.registry,
            )

        self._catalog = CatalogService(self.storage.meta, self._adapters)
        self._build_knowledge()
        self._build_skills()
        self._build_pipeline()
        return self

    # ------------------------------------------------------------ 技能装配
    def _build_skills(self) -> None:
        """装配技能目录（`SKILL.md`）。**失败不阻断启动**，但必须可见。

        技能是"声明式档"的文件格式：纯文本流程说明，按 description 匹配后
        整份加载。与知识库的分工是**事实 vs 流程**（见 `contracts/skill.py`）。

        装配期一次加载、运行期只读——技能在运行期被换掉会让
        "同一问题的行为"不可复现，而缓存指纹与回归测试都建立在可复现上。
        """
        from .skills import LoadedSkillCatalog, SkillLoader

        cfg = self.config.skills
        if not cfg.enabled:
            self.skill_catalog = LoadedSkillCatalog()
            self.skill_report = None
            return

        loader = SkillLoader(max_body_chars=cfg.max_body_chars)
        try:
            source_id = self.config.resolve_source().source_id
        except Exception:  # noqa: BLE001 - 配置未就绪时按全局处理
            source_id = None
        self.skill_catalog = loader.load(
            project_dirs=cfg.dirs,
            external_dirs=cfg.external_dirs,
            enabled_external=cfg.enabled_external,
            source_id=source_id,
        )
        self.skill_report = loader.report

    def skills(self) -> list[Any]:
        """已加载技能清单（管理界面用）。"""
        return self.skill_catalog.all() if self.skill_catalog else []

    # ------------------------------------------------------------ 知识库装配
    def _build_knowledge(self, *, auto_generate: bool = True) -> None:
        """装配知识库，并在有元数据快照时自动生成 schema 条目。

        注意时序：`connect()` 时元数据快照可能还不存在（要 `refresh_snapshot()`
        才有），因此自动生成**必须可重复调用**——它是幂等的，
        所以 `refresh_snapshot()` 之后会再触发一次，把库表结构补进知识库。
        """
        if not self.config.knowledge.enabled or self.storage is None:
            self._knowledge = None
            return

        from .knowledge import KnowledgeService

        if self._knowledge is None:
            self._knowledge = KnowledgeService(
                self.storage.knowledge,
                vector_store=self.storage.vector,
                hybrid=self.config.knowledge.hybrid,
            )
        if not (auto_generate and self.config.knowledge.auto_generate_from_comments):
            return
        for sid in self.config.sources:
            tables = self._catalog.tables(sid)
            if tables:
                self._knowledge.auto_generate_from_comments(sid, tables)

    # ------------------------------------------------------------ 管线装配
    def _build_pipeline(self) -> None:
        """装配查询管线：规划器 + 编译器 + 守门 + 缓存 + 编排。

        规划器选择（`config.agent.planner`）：
            `"rule"`   确定性规则规划器，零模型依赖
            `"llm"`    模型驱动
            `"hybrid"` **规则优先 + 模型兜底**（推荐，需注入 provider）
            `"auto"`   默认：有可用模型则 hybrid，否则 rule

        hybrid 的依据是实测：规则与模型覆盖不同的题，合并能提升准确率，
        同时把"确定的题"留在零延迟、零成本、可复现的路径上。
        """
        from .agent import AgentGraph, build_default_graph
        from .cache import CacheService
        from .dialect import SqlCompiler, SqlValidator
        from .guard import Guard
        from .llm import HybridPlanner, LLMPlanner, NullLLM, RulePlanner

        cfg = self.config.resolve_source()
        adapter = self._adapters[cfg.source_id]
        caps = adapter.capabilities

        model = self.semantic_model
        provider = self.llm_provider or NullLLM()
        llm_ready = bool(provider.available())

        mode = self.config.agent.planner
        if mode == "auto":
            # 默认选实测最优：有模型就用 **hybrid（规则优先 + 模型兜底）**。
            #
            # 依据（S3c 扩容后重测，黄金集 30 题，同一 Key）：
            #
            #   rule              25/30 = 83.3%   0 次调用   全部确定性
            #   hybrid            25/30 = 83.3%   ~1 次调用  29/30 确定性
            #   hybrid_verified   25/30 = 83.3%   ~30 次调用 不确定
            #   llm               23/30 = 76.7%   30 次调用  不确定
            #
            # 三种准确率并列，那么决策变量就只剩**代价与确定性**：
            # hybrid 用约 1 次调用拿到并列最高分，且 29/30 的题走确定性路径
            # （缓存指纹稳定、结果可复现、便于回归）。
            #
            # 注意这推翻了两件事：
            #   * 早先"llm 更准"的结论（那时语义层只会表达 21/30 种问法，
            #     规则规划器覆盖率不足才显得模型更强）——见 docs/07
            #   * "hybrid 准确率≈rule 但省调用"的定位：现在它的意义是
            #     **并列最高分 + 最低代价**，而不是"省钱但略差"
            #
            # 没有可用模型时退化为 rule（hybrid 没有模型就是 rule）。
            mode = "hybrid" if llm_ready else "rule"

        planner: object
        if mode == "llm" and llm_ready:
            planner = LLMPlanner(provider, model)
            self.planner_mode = "llm"
        elif mode == "hybrid_verified" and llm_ready:
            from .llm import PlanVerifier, VerifiedHybridPlanner

            planner = VerifiedHybridPlanner(
                RulePlanner(model), LLMPlanner(provider, model),
                verifier=PlanVerifier(provider, model),
            )
            self.planner_mode = "hybrid_verified"
        elif mode in ("hybrid", "hybrid_verified"):
            planner = HybridPlanner(
                RulePlanner(model), LLMPlanner(provider, model) if llm_ready else None,
            )
            self.planner_mode = "rule(hybrid 降级)"
        else:
            planner = RulePlanner(model)
            self.planner_mode = "rule"

        self._cache = CacheService(
            self.storage.cache,  # type: ignore[union-attr]
            enabled=self.config.cache.enabled,
            ttl_seconds=self.config.cache.ttl_seconds,
        )
        self._validator = SqlValidator(caps, self._catalog.tables(cfg.source_id))
        self._guard = Guard(caps, max_rows=self.config.safety.max_rows,
                            timeout_s=self.config.safety.timeout_s)
        self._pipeline_planner = planner

        self._agent = AgentGraph(
            build_default_graph(),
            planner=planner,
            compiler=SqlCompiler(caps),
            guard=self._guard,
            adapter=adapter,
            semantic_model=model,
            cache=self._cache,
            validator=self._validator,
            history=self.storage.meta,  # type: ignore[union-attr]
            knowledge=self._knowledge,
            max_rows=self.config.safety.max_rows,
            timeout_s=self.config.safety.timeout_s,
        )
        self._build_agent_runtime(mode, provider, model, adapter, caps)

    # ------------------------------------------------------------ agent 装配
    def _build_agent_runtime(self, mode: str, provider: Any, model: Any,
                             adapter: Any, caps: Any) -> None:
        """装配工具注册表与 agent 运行时。

        与线性管线的区别：这里注册的是**工具**，运行时会在循环里
        让模型自主选择调用哪个、以及失败后怎么改。线性管线本身
        被包装成 `nl2sql.solve` 工具——旧能力不丢，只是从"唯一路径"
        变成"其中一个选项"。
        """
        from .agent.builtin import BuiltinDeps, build_builtin_tools
        from .agent.runtime import AgentRuntime, RunConfig
        from .contracts.tool import CAPABILITIES, Capability
        from .dialect import SqlCompiler
        from .plugins import ToolRegistry
        from .plugins.loader import PluginLoader

        cfg = self.config.sources[self.config.resolve_source().source_id]
        adapter_caps = adapter.capabilities

        # 授予哪些能力由配置决定——**能力是显式授予的，不是默认可得的**
        granted_names = list(self.config.agent.capabilities)
        granted = tuple(
            Capability(name=n, granted_by="config")
            for n in granted_names if n in CAPABILITIES
        )
        registry = ToolRegistry(granted=granted, origin="core")

        self._tool_deps = BuiltinDeps(
            semantic_model=model,
            compiler=SqlCompiler(adapter_caps),
            guard=self._guard,
            adapter=adapter,
            cache=self._cache,
            catalog=self._catalog,
            knowledge=self._knowledge,
            planner=self._pipeline_planner,
            skill_catalog=self.skill_catalog,
        )
        registered, skipped = 0, []
        disabled_tools = set(self.config.agent.disabled_tools)
        for tool in build_builtin_tools(self._tool_deps):
            need = set(tool.spec.requires)
            if need - {c.name for c in granted}:
                # 能力未授予的工具不注册。它的 description 也就不会出现在
                # 模型的工具清单里——模型不会去调一个它看不见的工具。
                skipped.append(tool.spec.name)
                continue
            if tool.spec.name in disabled_tools:
                # 用户主动停用：同样不注册。这是**收紧**操作。
                skipped.append(tool.spec.name)
                continue
            registry.register(tool)
            registered += 1
        self.tool_skipped = skipped

        # 加载外部插件（可选）。单个插件坏掉不影响启动。
        plugins_dir = self.config.agent.plugins_dir
        self.plugin_errors: list[str] = []
        self.plugins_dir = plugins_dir
        self.plugin_manifests: list[dict[str, Any]] = []
        # 插件白名单：**空集 = 不加载任何外部插件**。
        #
        # 这里一开始写成了"空集 = 不设限 = 全加载"，方向是反的：
        # 插件是可执行代码，默认必须**不运行**，由用户显式点名才加载。
        # 想省事的用户可以显式写 `enabled_plugins = ["*"]`。
        allow_all = "*" in self.config.agent.enabled_plugins
        allowed_plugins = set(self.config.agent.enabled_plugins) - {"*"}
        if plugins_dir and (allowed_plugins or allow_all):
            loader = PluginLoader()
            for loaded in loader.load_all(plugins_dir):
                m = loaded.manifest
                if not allow_all and m.name not in allowed_plugins:
                    self.plugin_errors.append(
                        f"{m.name}: 不在 plugins.enabled 白名单中，未加载"
                    )
                    continue
                # 记下清单供管理界面展示（含未注册的工具规格）
                self.plugin_manifests.append({
                    "name": m.name,
                    "version": m.version,
                    "description": m.description,
                    "author": m.author,
                    "path": str(m.path) if m.path else "",
                    "requires": list(m.requires),
                    "warnings": list(loaded.warnings),
                    "tool_specs": [t.spec for t in loaded.tools],
                })
                # 插件能力不会超出它声明的范围；但**核心可以收回**：
                # 只授予配置里也允许的能力。
                plugin_caps = {c.name for c in loaded.capabilities}
                effective = plugin_caps & {c.name for c in granted}
                if plugin_caps - effective:
                    self.plugin_errors.append(
                        f"{m.name}: 能力 "
                        f"{sorted(plugin_caps - effective)} 未被配置授予，"
                        f"其工具不会注册"
                    )
                    continue
                for tool in loaded.tools:
                    # 停用的工具不注册——它的说明也就不进入模型的工具清单
                    if tool.spec.name in disabled_tools:
                        continue
                    try:
                        registry.register(tool,
                                          origin=f"plugin:{m.name}")
                        registered += 1
                    except (ValueError, PermissionError) as e:
                        self.plugin_errors.append(
                            f"{m.name}/{tool.spec.name}: {e}")
            self.plugin_errors.extend(loader.errors)

        self.tools = registry
        run_cfg = RunConfig(
            max_steps=self.config.agent.max_steps,
            max_repeat=self.config.agent.max_repeat,
            temperature=self.config.agent.temperature,
        )
        self._runtime = AgentRuntime(
            provider=provider, registry=registry, config=run_cfg,
            capabilities=granted,
            schema_brief=self._schema_brief(),
        )
        if mode == "agent":
            self.planner_mode = ("agent" if provider.available()
                                 else "rule(agent 降级：无模型)")

    def _schema_brief(self) -> str:
        """渲染库表事实，供决策 Prompt 直接引用。

        为什么直接喂而不是让模型去调工具：
            内部试用里问"这个库有什么数据"，模型从语义摘要推断出三张维度表，
            漏了 dim_product、事实表也说得含糊。它不算错（有依据），
            但明显不完整——而完整的表清单我们本来就有。
            把事实摆在 Prompt 里，比要求模型"先去查"更省一轮调用也更准。

        只给"表名 + 行数 + 列名"，不给类型与注释：
        决策阶段够用，且能控制 Prompt 体积。
        """
        if self._catalog is None:
            return ""
        lines: list[str] = []
        for sid in self.config.sources:
            tables = self._catalog.tables(sid)
            if not tables:
                continue
            if len(self.config.sources) > 1:
                lines.append(f"[数据源 {sid}]")
            for t in tables:
                cols = "、".join(c.name for c in t.columns)
                rows = f"{t.row_estimate} 行" if t.row_estimate is not None else "行数未知"
                lines.append(f"- {t.name}（{rows}）：{cols}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 运行期管理
    def apply_runtime_settings(self) -> None:
        """把 `AgentConfig` 的运行期参数同步到已装配的组件。

        **这是"改配置无需重启"的落点**：循环参数与 Prompt 模板都不涉及
        模块导入，所以可以就地更新。插件代码的增删不在此列（需重启）。
        """
        if self._runtime is None:
            return
        cfg = self._runtime.config
        cfg.max_steps = self.config.agent.max_steps
        cfg.max_repeat = self.config.agent.max_repeat
        cfg.temperature = self.config.agent.temperature
        self._runtime.set_system_prompt(self._load_decision_prompt())
        self._runtime.set_schema_brief(self._schema_brief())

    def _load_decision_prompt(self) -> str:
        """加载外置决策 Prompt。读不到时回退内置默认，**但不再静默**。

        P0.5 修的就是这个"静默"：原来文件不存在、路径写错、读不了都
        `return ""`，用户以为自己的 Prompt 生效了，其实用的是内置的。
        那与本项目反复强调的"不静默"原则直接冲突。

        现在仍然回退（可用性优先：用户可能刚删了文件），
        但把原因记进 `skill_report.prompt_issues`，透出到管理界面。
        """
        spec = (self.config.agent.prompt_files or {}).get("decision")
        if not spec:
            return ""
        if not os.path.isfile(spec):
            self._note_prompt_issue(
                f"decision Prompt 路径不存在: {spec}——已回退内置 Prompt"
                f"（你的外置 Prompt **没有生效**）"
            )
            return ""
        try:
            with open(spec, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            self._note_prompt_issue(
                f"decision Prompt 读取失败: {spec}（{e}）——已回退内置 Prompt"
            )
            return ""
        if not text.strip():
            self._note_prompt_issue(
                f"decision Prompt 是空文件: {spec}——已回退内置 Prompt"
            )
            return ""
        return text

    def _note_prompt_issue(self, message: str) -> None:
        """把 Prompt 加载问题挂到报告上，让界面能显示。"""
        if self.skill_report is None:
            # 技能机制关掉时也要能透出 Prompt 问题，所以单独兜一份
            from .skills import SkillLoadReport

            self.skill_report = SkillLoadReport()
        self.skill_report.prompt_issues.append(message)

    def all_tool_specs(self) -> list[Any]:
        """工具规格**全集**：内置 + 插件，含未注册的。

        管理界面靠它展示"有哪些可启用"。若只看注册表，被停用或
        未授权的工具就消失了，用户找不到它们，也就无从恢复。
        """
        from .agent.builtin import builtin_tool_specs

        specs: list[Any] = list(builtin_tool_specs())
        seen = {s.name for s in specs}
        for manifest in getattr(self, "plugin_manifests", []) or []:
            for spec in manifest.get("tool_specs", []):
                if spec.name not in seen:
                    specs.append(spec)
                    seen.add(spec.name)
        return specs

    def rebuild_toolset(self) -> None:
        """按当前配置重新装配工具集（能力或停用清单变更后调用）。

        与首次装配的区别：**不重新导入插件模块**——
        Python 卸载已导入模块有诡异问题（旧引用仍活、单例残留），
        个人工具不值得冒这个险。插件代码的增删请重启进程。
        """
        if self._agent is None or self.storage is None:
            return  # 尚未 connect
        cfg = self.config.resolve_source()
        adapter = self._adapters.get(cfg.source_id)
        if adapter is None:
            return
        from .llm import NullLLM

        provider = self.llm_provider or NullLLM()
        mode = self.config.agent.planner
        if mode == "auto":
            mode = "llm" if provider.available() else "rule"
        self._build_agent_runtime(mode, provider, self.semantic_model,
                                  adapter, adapter.capabilities)
        self.apply_runtime_settings()

    # ------------------------------------------------------------ 内置注册
    def _register_builtins(self) -> None:
        """注册内置实现。外部可在构造前用 `registry` 参数覆盖或新增。"""
        from .sources.sqlite import SqliteAdapter

        if not self.registry.adapters.has("sqlite"):
            self.registry.adapters.register("sqlite", SqliteAdapter)

        from .storage.memory_vector import HashEmbedder, MemoryVectorStore

        if not self.registry.embedders.has("hash"):
            self.registry.embedders.register("hash", HashEmbedder)
        if not self.registry.vector_stores.has("memory"):
            self.registry.vector_stores.register("memory", MemoryVectorStore)

    # ------------------------------------------------------------ 门面方法
    def ask(
        self,
        question: str,
        *,
        source_id: str | None = None,
        now: str | None = None,
        use_cache: bool | None = None,
        session_id: str | None = None,
    ) -> QueryOutcome:
        """自然语言 → SQL + 结果表。agent 模式下走工具循环。

        Args:
            question: 业务问题（自然语言）。
            source_id: 数据源标识；None 用默认数据源。
            now: 相对时间锚点（ISO 日期）。评测/复现时必须显式传入。
            use_cache: 覆盖配置中的缓存开关。
            session_id: 会话标识。**agent 模式（尤其澄清）必须传**，
                否则下一轮接不上上下文。不传时自动生成单轮会话。

        Returns:
            `QueryOutcome`。失败不抛异常，而是带 status/message/suggestions。
            agent 模式下可能返回 `status="need_clarification"`——
            这是**正常状态**：智能体在信息不足时选择不猜。
        """
        source_cfg = self.config.resolve_source(source_id)
        resolved_now = now or self.config.now

        self.connect()
        if self._agent is None:
            return self._not_ready(question, source_cfg.source_id, resolved_now)

        # agent 模式：交给运行时循环
        if (self.config.agent.planner == "agent"
                and self._runtime is not None
                and self._runtime.available()):
            return self._run_agent(
                question, source_id=source_cfg.source_id,
                now=resolved_now, session_id=session_id,
            )

        return self._agent.run(
            question, source_id=source_cfg.source_id, now=resolved_now,
            use_cache=use_cache,
        )

    # ------------------------------------------------------------ agent 运行
    def _run_agent(self, question: str, *, source_id: str, now: str | None,
                   session_id: str | None) -> QueryOutcome:
        """跑一轮 agent 循环，并负责会话的读写与资源注入。"""
        import uuid

        from .contracts.session import SessionState

        store = self.storage.sessions if self.storage else None
        sid = session_id or uuid.uuid4().hex[:16]
        session = store.load(sid) if store is not None else None
        if session is None:
            session = SessionState(session_id=sid, source_id=source_id)
        # 时间锚点：会话已有时沿用，避免用户回答澄清问题后「本月」漂移
        session.now = session.now or now
        session.source_id = source_id

        adapter = self._adapters.get(source_id)
        resources = {
            "semantic_model": self.semantic_model,
            "compiler": self._tool_deps.compiler,
            "guard": self._guard,
            "adapter": adapter,
            "cache": self._cache,
            "catalog": self._catalog,
            "knowledge": self._knowledge,
            "planner": self._pipeline_planner,
            "skill_catalog": self.skill_catalog,
            "max_rows": self.config.safety.max_rows,
        }

        def _pipeline(q: str, ctx: Any) -> QueryOutcome:
            """把线性管线暴露给 `nl2sql.solve` 工具。

            复用现有 `AgentGraph`，因此旧能力（校验、缓存、结果组装）
            一样生效——升级为 agent 不等于丢掉原来的东西。
            """
            assert self._agent is not None
            return self._agent.run(
                q, source_id=source_id, now=session.now,
                use_cache=self.config.cache.enabled,
            )

        resources["pipeline"] = _pipeline
        outcome = self._runtime.run(question, session=session,
                                    resources=resources)
        outcome.plan_fingerprint = outcome.plan_fingerprint or None
        if store is not None:
            try:
                store.save(session)
            except Exception:  # noqa: BLE001 - 会话保存失败不该让问答失败
                pass
        # 把会话标识透出，界面下一轮要带上它
        outcome.notes.append(f"会话: {sid}")
        return outcome

    def new_session_id(self) -> str:
        import uuid

        return uuid.uuid4().hex[:16]

    def _not_ready(self, question: str, source_id: str, now: str | None) -> QueryOutcome:
        """保留的安全网：管线未装配时明确告知，而不是抛异常或返回假数据。"""
        return QueryOutcome(
            status="error", question=question, source_id=source_id,
            error_code="NOT_IMPLEMENTED",
            message="查询管线未装配。",
            suggestions=["检查 NL2SQLConfig 中数据源与语义模型配置"],
            notes=[f"nl2sql {__version__}"],
        )

    # ------------------------------------------------------------ 只读能力
    def sources(self) -> list[str]:
        return sorted(self.config.sources)

    def capabilities(self, source_id: str | None = None) -> dict[str, Any]:
        """返回某数据源的能力声明，供壳层做界面降级。"""
        cfg = self.config.resolve_source(source_id)
        self.connect()
        adapter = self._adapters[cfg.source_id]
        caps = adapter.capabilities
        return {
            "dialect": caps.dialect,
            "sql": caps.sql,
            "pagination": caps.pagination,
            "supports_cte": caps.supports_cte,
            "supports_window": caps.supports_window,
            "identifier_quote": caps.identifier_quote,
        }

    def test_connection(self, source_id: str | None = None) -> dict[str, Any]:
        """连接自检。返回结构化结果而不是抛异常，便于壳层直接展示。"""
        cfg = self.config.resolve_source(source_id)
        self.connect()
        adapter = self._adapters[cfg.source_id]
        try:
            adapter.test_connection()
        except Exception as e:  # noqa: BLE001 - 自检失败是预期情况
            return {"ok": False, "source_id": cfg.source_id, "message": str(e)}
        return {"ok": True, "source_id": cfg.source_id, "dialect": adapter.capabilities.dialect}

    def tables(self, source_id: str | None = None) -> list[TableMeta]:
        """列出某数据源的表（来自本地快照，不实时连库）。"""
        cfg = self.config.resolve_source(source_id)
        self.connect()
        return self._catalog.tables(cfg.source_id)

    def refresh_snapshot(self, source_id: str | None = None, *,
                         force: bool = False) -> Any:
        """抽取/刷新元数据快照。随后把库表结构补进知识库与决策 Prompt。"""
        cfg = self.config.resolve_source(source_id)
        self.connect()
        meta = self._catalog.refresh(cfg.source_id, force=force)
        # 快照就绪后再触发一次自动生成（幂等，不会覆盖用户编辑）
        self._build_knowledge()
        # ★ 关键：装配管线时快照可能还不存在（首次启动），
        # 那时的 `schema_brief` 是空的。快照落盘后必须重新同步一次，
        # 否则模型永远看不到库表清单——这个漏同步真实导致过
        # "问库里有什么数据时漏表、事实表说不清"。
        self.apply_runtime_settings()
        return meta

    def ddl(self, source_id: str | None = None, **kwargs: Any) -> str:
        """渲染受限的精简 DDL（**不要全量塞 prompt**）。"""
        cfg = self.config.resolve_source(source_id)
        self.connect()
        return self._catalog.render_ddl(cfg.source_id, **kwargs)

    def history(self, *, source_id: str | None = None, limit: int = 50) -> list[Any]:
        """查询历史（个人场景的体验组成部分：可回看、可复用）。"""
        self.connect()
        sid = source_id or (
            self.config.resolve_source().source_id if self.config.sources else None
        )
        return self.storage.meta.list_history(source_id=sid, limit=limit)  # type: ignore[union-attr]

    # ------------------------------------------------------------ 知识库
    def _require_knowledge(self) -> Any:
        self.connect()
        if self._knowledge is None:
            raise ConfigError("知识库未启用（config.knowledge.enabled = False）")
        return self._knowledge

    def knowledge_upsert(self, entry: KnowledgeEntry, *, note: str = "") -> KnowledgeEntry:
        """写入知识条目（免审，但留版本）。"""
        return self._require_knowledge().upsert(entry, note=note)

    def knowledge_upsert_many(self, entries: list[KnowledgeEntry], *,
                              note: str = "") -> list[KnowledgeEntry]:
        return self._require_knowledge().upsert_many(entries, note=note)

    def knowledge_list(self, *, source_id: str | None = None,
                       types: tuple[Any, ...] = (),
                       include_disabled: bool = False) -> list[KnowledgeEntry]:
        return self._require_knowledge().list(
            source_id=source_id, types=types, include_disabled=include_disabled)

    def knowledge_set_enabled(self, key: str, enabled: bool) -> None:
        """停用/启用某条知识。怀疑它污染结果时可立即下线。"""
        self._require_knowledge().set_enabled(key, enabled)

    def knowledge_delete(self, key: str) -> None:
        self._require_knowledge().delete(key)

    def knowledge_history(self, key: str) -> list[Any]:
        return self._require_knowledge().history(key)

    def knowledge_rollback(self, key: str, version: int) -> KnowledgeEntry:
        return self._require_knowledge().rollback(key, version)

    def knowledge_test_retrieve(
        self, text: str, *, source_id: str | None = None, top_k: int = 8,
    ) -> list[RetrievalHit]:
        """测试检索：查看某问题实际命中了哪些知识条目、为什么命中。

        这是知识库可用性的唯一可靠验证方式——没有它，
        "上传的知识是否生效"只能靠猜。
        """
        return self._require_knowledge().test_retrieve(
            text, source_id=source_id, top_k=top_k)

    def knowledge_few_shot(self, question: str, *, source_id: str | None = None,
                           top_k: int = 3) -> list[tuple[str, str]]:
        """取相似的历史 (问题, SQL) 对（供排查与调试）。"""
        return self._require_knowledge().few_shot_examples(
            question, source_id=source_id, top_k=top_k)

    def knowledge_reindex(self) -> int:
        """全量重建向量索引。换 embedding 模型后必须调用。"""
        return self._require_knowledge().reindex()

    def close(self) -> None:
        """释放适配器连接与存储句柄。壳层应在退出时调用。"""
        for adapter in self._adapters.values():
            try:
                adapter.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应阻断退出
                pass
        self._adapters.clear()

        if self.storage is not None:
            from .storage import close_storage

            close_storage(self.storage)
            self.storage = None
        self._catalog = None
        self._knowledge = None
        self._cache = None
        self._agent = None

    def __enter__(self) -> "NL2SQL":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()
