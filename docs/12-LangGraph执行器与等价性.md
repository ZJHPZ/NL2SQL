# 12 · LangGraph 执行器与等价性（M2）

> 这一章记录 M2：课题名字里的"基于 LangGraph"从一句话变成**可选可验证**的
> 调度方式，同时保留原来"自己写的循环"作为等价性基准。
>
> 结论先放这里：**两个执行器在 6 个脚本化场景上逐字段一致（含事件序列）**；
> 30 题真模型硬验收改为**录制—重放**并已通过（30/30 一致）；
> 代价是每轮多 ~9ms 调度开销、启动多 3.6s 导入。
>
> 最值钱的不是"接上了 LangGraph"，而是**两次被测试工具骗了**：
> 一次是 LangGraph 按签名注解决定要不要注入 config（§5.1），
> 一次是 Starlette 的 TestClient 会把整个 SSE 响应缓冲完再交给你（§5.5）。
> 两个问题都表现为"代码看起来对、数字不对"。

---

## 1. 为什么不是"再写一个循环"

最省事的做法是复制 `nl2sql/agent/runtime.py` 的循环体，把 `while` 换成
`StateGraph` 的四条边，两百行改一百行，当天就能跑。

**没有这么做**，理由是：复制两份之后，差异**不会在第一次跑通时暴露**，
而是半年后有人改了一道护栏——改了一边、忘了另一边，两边都"能跑"，
只有某类问题开始给出不同答案时才有人发现。那时已经没人记得"这两份应该一致"。

所以 M2 的第一个动作是**抽单步状态机**：

```
nl2sql/agent/machine.py   ← 所有判断（决策解析、7 道护栏、结果挑选、收尾）
nl2sql/agent/runtime.py   ← 调度 A：while 循环（等价性基准，零第三方依赖）
nl2sql/agent/graph.py     ← 调度 B：StateGraph 四个节点 + 条件边
nl2sql/agent/factory.py   ← 按配置选，降级要留痕
```

`machine.py` 里有 `AgentCore`：`decide()` / `review()` / `act()` / `finish()` /
`to_outcome()` 五个方法，每个方法**只做一步**并返回一个路由常量
（`review` / `tool` / `continue` / `final` / `clarify` / `stop` / `retry` /
`exhausted`）。两个执行器都只是"按返回值决定下一步调谁"。

### 1.1 一个刻意的约束：状态是纯 dict

`AgentCore` 的状态是 `dict[str, Any]`，不是 dataclass。原因有两条：

* 决策要落进状态（`state["decision"] = decision.to_dict()`），而 LangGraph
  的 checkpoint 只接受可序列化数据；
* 图的节点返回"部分状态"，框架按键合并——纯 dict 语义干净。

代价是丢类型检查。这个代价用测试补：两个执行器跑同一串脚本，逐字段比对。
**如果某天判断漏进了某个执行器，这个测试会红，而类型检查不会。**

### 1.2 上下文不进图状态

`RunContext`（模型 provider、工具注册表、事件回调、会话）**不放图状态**，
而是走 LangGraph 的 `config["configurable"]["rctx"]`。理由：
一旦进状态，checkpoint 就要序列化 provider 和 sqlite 连接，必然失败。
这条边界在 `_AgentNode.run` 里守卫——拿不到 `RunContext` 直接抛错，
不静默跑一半。

---

## 2. 图长什么样

`build_agent_graph_spec()` 返回**纯数据** `GraphSpec`（4 节点 / 9 条边），
可以展示、可以画、可以被测试断言：

```
decide ──REVIEW──────► review ──TOOL──────► act
  │                      │                  │
  │                      ├─RETRY──► decide  ├─CONTINUE──► decide
  │                      ├─FINAL──► finish  └─STOP──────► finish
  └─EXHAUSTED──► finish  └─CLARIFY► finish
                                     │
                                     └──► __end__
```

四件值得说的事：

1. **护栏是图上的一个节点**（`review`），不是散落在循环体里的 `if`。
   这样一来"顶回去重来"在图上有名字，在 trace 里看得见（`guard` 事件）。
2. `finish` 有两个入口（步数耗尽、模型给出结论），语义不同但收尾动作一样，
   所以合成一个节点，靠 `state["status"]` 区分。
3. **每条路由常量都必须有边接住**。测试会遍历 `Route` 常量逐条断言——
   漏一条边的表现是"图跑到某个节点后静默停住"，最难受的那种 bug。
4. `describe()` 把节点与边吐给 `/api/info`，前端在顶栏把执行器名和边数
   写在 tooltip 里（§6.3）。图不是画给人看的装饰，是**可被验证的实现**。

通用编译器 `build_langgraph(spec, nodes)` 与 agent 无关：给它 `GraphSpec`
和节点映射就能编译任意图。同一节点混用条件边与无条件边会**直接报错**——
那是语义歧义，不该由框架猜。

---

## 3. 等价性怎么验的

### 3.1 场景级（离线，已通过）

`tests/test_m2_langgraph.py` 把一串**预定的决策**同时喂给两个执行器，
比对：状态、散文答案、SQL、notes、建议、行数、列名、全部行、缓存标记、
trace 键、**事件序列**（去掉耗时字段）、模型调用次数。

| 场景 | 覆盖的路由 | 结果 |
|---|---|---|
| `tool_final` | REVIEW→TOOL→CONTINUE→FINAL | 一致 |
| `two_tools` | 两次查询，保留**最丰富**的结果 | 一致 |
| `clarify` | REVIEW→CLARIFY→STOP | 一致 |
| `repeat` | 打转护栏（RETRY） | 一致 |
| `tool_error` | 工具失败后模型换路 | 一致 |
| `exhausted` | EXHAUSTED→finish | 一致 |

比"结果一致"更强的一条断言是**事件序列一致**：过程一样，结果才可能长期一样。
只比最终结果的话，两个执行器完全可能"用不同的步骤到达同一个答案"，
而这种差异在换模型、换步数上限时就会变成结果差异。

### 3.2 30 题硬验收（已通过：录制—重放）

**第一版设计不可靠，被真模型证伪。** 第一版让两个执行器各自调用真模型
跑一遍 30 题再逐字节比。实测 4 题不一致，但探针证明根因不在执行器：
同一执行器同一题连问两次结果稳定；两个执行器第一轮喂给模型的 Prompt
**完全相同**，模型两次回复却不同（temperature=0 下 DeepSeek 端点仍会
run-to-run 抖动；返回的 `model` 字段甚至变成 `deepseek-flash`，而请求的
是 `deepseek-chat`）。真模型输出本身非确定，"两边各跑一遍再逐字节比"会把
模型随机性误判成执行器的 bug，也不可能稳定通过。

改成**录制—重放**（把"模型决策"与"执行器调度"拆开验）：

1. `loop` 执行器跑一遍真模型，按题录下每次 Prompt 与回复；
2. `langgraph` 执行器重放同一串回复，重放时断言 Prompt **逐字一致**；
3. 逐题比 `status / sql / error_code / row_count / rows / columns`。

**结果（30 题，model=deepseek-chat，max_steps=8）**：30/30 题一致，
重放 Prompt 0 偏差；录制轮多数题 2 次模型调用，个别 6 次。
重放模式下 langgraph 不再联网，全部调用打在本机。

跑法：

```bash
set NL2SQL_LLM_API_KEY=...          # 只在本机环境变量里，不进仓库
python tests/test_m2_langgraph.py --require-live
```

默认（不带 `--require-live`）无 Key 时只打印"未跑 ≠ 通过"并返回 0，
以免它挡住无关的离线回归；带 `--require-live` 时跳过即失败。

#### 3.2.1 打真模型打出来的两个真问题（都已修）

1. **递归预算公式写小了**。旧公式 `recursion_limit = 2*max_steps + 5`，
   `max_steps=8` 时只有 21 跳；但一轮循环最多走 `decide → review → act`
   三个节点，8 轮"每轮调一次工具"要 `8×3 + 2 = 26` 跳——模型还没到业务
   步数上限，框架先抛 `GraphRecursionError`（多轮会话里更明显）。改为
   `3*max_steps + 5`，并把 `RecursionError` 兜底成
   `error_code="RECURSION_LIMIT"` 的业务结果（提示可读、可重试），而不是
   把框架内部异常抛给调用方。新增第 [7] 段「递归预算」：用 8 次不同参数的
   工具调用压满 8 步，断言不撞框架上限。
2. **模型端点会换后端**。请求 `deepseek-chat`，响应 `model` 却是
   `deepseek-flash`。功能上没坏，但意味着"同一请求两次可能落到不同模型"，
   这解释了 run-to-run 抖动；评测记录里要记响应 model，而不是只记请求 model。

### 3.3 事件契约也是验收对象

界面的"正在查语义层"依赖事件字段，所以单独验：`decision` 事件带
`step`/`kind`，`tool` 事件带工具名与成功标志，`outcome` 事件在末尾。
另外验了一条容易被忽略的性质：**观察者（界面回调）抛异常不影响查询**——
事件是旁路，不是控制流。

---

## 4. 实测数据

| 指标 | 值 | 说明 |
|---|---|---|
| 首次 `import langgraph` | **3566 ms** | 一次性；发生在第一次 `compile()` |
| `compile()`（已导入） | 4.3–4.7 ms | 每进程一次 |
| 每轮调度开销（loop） | **0.18 ms**（中位，n=20） | 纯 Python while |
| 每轮调度开销（langgraph） | **9.2 ms**（中位，n=20） | 约 51× |
| 一次真实提问（agent，~9s） | 见 docs/03 | 调度开销占比 <0.1%，可忽略 |
| SSE 首字节（ASGI 生成器层） | **24.6 ms** | 查询本身 1.0s 时测得 |
| SSE 首字节（真 uvicorn + HTTP） | **<500 ms**（测试门槛，实测远低于） | 查询 1.2s 时 |

两条诚实的解读：

* **9ms 不是性能问题，但也不是零**。相对一次 9 秒的模型问答可以忽略；
  相对"规则规划器 1ms 出结果"就是 9 倍。所以 `auto` 只在 agent 模式下
  才选图执行器，线性管线根本不经过这里。
* **3.6 秒的导入落在第一次查询上**。选择 `NL2SQL_RUNTIME=langgraph` 的
  用户，第一次提问会先等这个导入。没有做启动预热（预热会让不用图执行器的
  用户白等 3.6 秒）。这是有意的取舍，写在这里以免被当成"偶发卡顿"。

---

## 5. 踩的坑（都表现为"代码看起来对、数字不对"）

### 5.1 LangGraph 按**签名注解**决定要不要注入 config

节点签名写成 `def run(state, config: Any)`，调用时报：

```
TypeError: _wrap_node.<locals>._call() missing 1 required positional argument: 'config'
```

原因：LangGraph 只认参数名为 `config`、注解为 `RunnableConfig` /
`Optional[RunnableConfig]`（或**完全不注解**）的函数才传 config。
写 `Any` 被当成"这个函数不要 config"。

解法：`graph.py` 用 `from __future__ import annotations` + `TYPE_CHECKING`
导入，注解在运行期是字符串 `"RunnableConfig"`，正好命中它接受的写法，
而运行期不需要真的 import。**坑的教训**：框架用签名做分派时，
类型注解就是运行时契约，不是文档。

### 5.2 `emit` 的参数名与载荷字段撞了

`emit(kind, **payload)` 而载荷里也有 `kind`（决策类型）：

```
TypeError: AgentCore.emit() got multiple values for argument 'kind'
```

改成 `emit(event, *, rctx=None, **payload)`，事件名与载荷彻底分开。

### 5.3 事件接收器不能挂在 `AgentCore` 上

一个引擎只有一个 `AgentCore`。若把回调存在 core 上，两路并发请求会串线
（甲请求的步骤推进到乙的界面）。回调放在 `RunContext.on_event`——
每次 `run()` 一个上下文，天然隔离。

### 5.4 空 TypedDict 会让状态丢成 None

`StateGraph(SomeEmptyTypedDict)` 编译能过，运行时状态被置成 `None`。
用 `StateGraph(dict)` 且节点只返回部分字段，框架按键合并，行为符合预期。

### 5.5 Starlette 的 TestClient 会把整个 SSE 响应缓冲完

第一版首字节测试用 `TestClient.stream()`，结果 1.213s 才收到第一行——
看起来像"首字节没生效"。单独用一个"先 yield 再 sleep 1s"的最小应用验证：

```
1.008s  event: start     ← 生成器第一时间 yield，但调用方 1.008s 才拿到
```

**Starlette 1.0.1 的 TestClient 不流式**：它会等整个响应结束才把 body 交给
调用方。所以首字节**必须用真 uvicorn + 真 HTTP 量**（测试里起线程内
uvicorn，用 `http.client` 逐行读）。改完之后：查询 1.2s，首字节 <0.5s。

这条坑的普适教训：**测量工具本身也要被验证**。如果当时信了 1.213s，
"修"的方向会是错的（去改生成器——而生成器本来就是对的）。

---

## 6. 选择、降级与展示

### 6.1 三种取值

| `NL2SQL_RUNTIME` | 行为 |
|---|---|
| `auto`（默认） | 装了 langgraph 就用图执行器，否则用循环 |
| `loop` | 强制循环（等价性基准；零第三方依赖） |
| `langgraph` | 强制图执行器；**没装时降级为循环并留痕** |

认不出的名字**直接报错**，不默默退回。与 `resolve_secret` 处理未知
`credential_ref` 同一条原则：配置写错时，用户需要知道，而不是拿到一个
"能跑但不知道为什么"的系统。

### 6.2 降级必须留痕

没装 langgraph 却要图执行器时，`make_runtime` 返回 `(循环执行器, 说明)`，
说明里点名缺什么、怎么装。这条说明会出现在：

* `nl2sql/api.py` 的 `runtime_note`
* `/api/info` 的 `runtime.note`，并追加进 `startup_warnings`
* 前端顶栏的"已降级"标记（tooltip 里是原文）

理由：静默换执行器是最坏的一种诚实问题——用户以为在跑图执行器，
论文里也会写成"基于 LangGraph"，而实际跑的是 while 循环。

### 6.3 界面露出

`/api/info` 增加 `runtime: {name, requested, note, available, graph}`。
前端顶栏在"规划器"旁边显示"执行器 自写循环 / LangGraph"，
tooltip 里给出图结构（节点链 + 边数）或"零依赖 while 循环"的说明。
`planner_mode` 回答"用哪种方式理解问题"，`runtime` 回答"用哪种方式调度步骤"——
两件事都要能看见，否则出问题时无法定位是哪一层。

### 6.4 为什么事件不是 `astream_events` 产出的

M2 计划里写的是"用 LangGraph 的 `astream_events` 做流式"。实现时改了：

* `astream_events` 只有图执行器有。若 SSE 依赖它，`NL2SQL_RUNTIME=loop`
  的用户就没有流式——而 M2 的硬验收是"两个执行器等价"。
* 等价性不止是最终结果，**过程也要一样**（§3.1 比事件序列就是为了这个）。
  如果一条路的事件来自框架、另一条来自手写回调，字段迟早分叉。

所以事件由 `AgentCore` 在**单步边界**发（`decision` / `tool` / `guard` /
`clarify` / `outcome`），执行器只负责把 `RunContext.on_event` 接上；
SSE 端点对执行器无感知，两条路走同一个中继。

代价：拿不到框架内部的细粒度事件（token 级流式、节点进入/退出）。
**当前不需要**——用户等的是"哪一步在跑"，不是逐 token。
真要 token 流式，应该从模型客户端层做，而不是从图框架层做（`chat()` 一次
返回完整 `LLMResponse`，这一层本来就没有 token 事件）。

---

## 7. 局限与下一步

1. **录制—重放证明的是"同一串模型决策下调度等价"**，不是"两个执行器
   各自调真模型会得到一样的结果"。后者被实测否定（同 Prompt 两次回复就
   不同），也不该作为验收标准——模型随机性是模型的事，执行器要对齐的是
   调度语义。真要控制随机性，得让模型服务端给 deterministic 保证（seed/
   固定后端），当前 DeepSeek 端点做不到（请求 deepseek-chat 响应
   deepseek-flash）。
2. **checkpoint 只用到了"能接上"**。`MemorySaver` 测试通过、两轮不串线，
   但"进程重启后从第 4 步接着跑"没有做——那需要把 `RunContext` 也持久化
   （或重建），是另一件事。**图执行器当前的收益是可观测/可扩展，
   不是"断点续跑"**，文档不替它吹这个。
3. **图的结构还不能在界面上编辑**。`docs/08` 的能力视图做到了
   "编辑/启用/风险提示"，图节点还是代码里定义的。可编辑图是个诱人但危险的
   方向（用户改图 → 某条路由没边接 → 静默停住），真要做需要先有
   图校验器，`build_langgraph` 的"同一节点不许混用边"只是最小的一条。
4. **每轮 9ms 的调度开销**没有优化。真要优化得把节点函数改成纯函数、
   由执行器负责状态合并，那是另一个抽象层次的事，当前不值得。

---

## 附：文件与测试对应

| 文件 | 内容 |
|---|---|
| `nl2sql/agent/machine.py` | 单步状态机（全部判断，~900 行） |
| `nl2sql/agent/runtime.py` | 调度 A：while 循环（~150 行，薄） |
| `nl2sql/agent/graph.py` | 调度 B：`GraphSpec` → `StateGraph`，通用编译器 |
| `nl2sql/agent/factory.py` | 选择与降级 |
| `apps/web/backend/routers/query.py` | `/api/query/stream`（SSE） |
| `apps/web/backend/routers/info.py` | 暴露执行器与图 |
| `tests/test_m2_langgraph.py` | 工厂 / 图 / 编译器 / 等价性 / 事件 / checkpoint / 递归预算 / 首字节 / 30 题录制—重放 |
