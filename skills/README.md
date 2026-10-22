# 技能目录（`SKILL.md`）

这里是**声明式档**：只写文本、不写代码就能改变 agent 的行为。

相比另外两档：

```
声明式（技能，本目录）  →  工具（plugins/）  →  提供者（实现 Protocol）
只写 Markdown             写一个工具函数        实现一个接口
```

完整设计见 **`../docs/08-技能设计草案.md`**，参考实现对照见
`../docs/09-参考实现-superpowers.md`。

> **当前状态：P0 机制已通，P1 项目 playbook 已加入（3 份）。**
> `cross-period-comparison`（跨期对比）、`having-vs-group-filter`
> （HAVING vs 行级过滤）、`unverified-metric-disclosure`（口径未注册时披露）。
> 有技能时 `skill.load` 才会注册；目录由壳层注入 system prompt（引导注入，
> 见 `nl2sql/agent/machine.py`）。技能是否真的改变决策，用
> `scripts/decision_eval.py` 的基线对照实验测（不进 `tests/`）。
> 实测（2026-10，4 次）：选择 11/12 稳定；正确率差值 −2…+1 在噪声内
> （场景集偏简单，失败多是步数上限未收敛）——数据在
> `data/_experiments/decision/`，结论见 `../docs/08-技能设计草案.md` §13。

## 两种放置方式

```
skills/
  <name>/SKILL.md      # 目录式打包（可带 references/ templates/ 等附属文件）
  <name>.md            # 或平铺单文件
```

本文件（`README.md`）不会被当成技能：平铺 `.md` 缺 frontmatter 时按普通文档忽略。
但 `SKILL.md` 缺 frontmatter **会报错**——文件名已经表明了它的意图。

## 格式

```markdown
---
name: period-comparison
description: 用户把两个时间段并排提问时使用（"本月和上月""同比""环比"）
nl2sql:
  kind: playbook            # playbook（默认）| planner-hint
  requires: [semantic.read] # 复用既有能力名
  scope: shop               # 绑数据源；省略 = 全局
  enabled: true
  applies_to:               # 给无模型路径做确定性匹配
    - "本月.*上月"
---

# 跨期对比

## 何时用
…

## 步骤
…
```

## 四条硬规则（都有实测依据）

1. **`description` 只写"何时用"，绝不概括流程或产出。**
   描述里概括了流程，模型会照着描述做而**不去读正文**——这是踩过的坑。
   frontmatter 总长上限 1024 字符，`description` 建议 < 500。

2. **正文说"动作"，不说工具名。** 写"确认指标口径"，
   不写 `semantic.describe`。因为**能力没授予的工具根本不注册**，
   写死工具名的技能在那种场景下会教模型去调一个不存在的工具。
   动作 → 工具的翻译由各个壳负责。

3. **必须写降级措辞。** 工具缺失时指示模型"换个做法或如实说缺什么"，
   而不是照旧指示去调用。

4. **正文整份加载，不截断。** 所以超限是**拒绝加载**而不是截断——
   流程被切一半就不成立了。重型参考请拆到同目录的附属文件里，正文只留步骤。

## 外部技能默认不加载

`.dsh/skills`、`.agents/skills`、`~/.dsh/skills` 里的技能
（例如 `npx skills add` 装的）**默认一个都不加载**，需要在
`config/nl2sql.toml` 里点名：

```toml
[skills]
enabled = ["some-skill"]   # 空 = 不加载；"*" = 全部
```

与插件那边"默认不运行别人的代码"是同一条原则，
平移到"默认不加载别人的指令"。
