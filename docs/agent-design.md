# 校验智能体设计

## 1. 定位

对已建成的双库（Qdrant chunk 向量 + Neo4j 图谱）做**逐段落校验**的自治智能体：以每个 chunk 为一次任务起点，比对"原文 ↔ 图谱子图 ↔ 向量召回"，输出结构化校验结论并写回图谱（权重/描述/关系/时序）。长期运行沉淀经验到 agent/domain-knowledge.md，形成"越校验越准"的飞轮。

## 2. 运行时架构

```
驱动器 runner.py（普通脚本, 遍历全部 chunk, 断点续跑）
  └─ 每 chunk 一个 session:
       AgentScope ReActAgent
       ├─ system = domain-knowledge.md(可选领域知识) + node-validation.prompt.md(任务规范)
       ├─ Toolkit(mcps=[graph_read, graph_write, rag])
       ├─ 模型: MiniMax-M3（与管线一致, 64k max_tokens 防长思考截断）
       └─ OTLP → Langfuse: trace_id = chunk_id（一段落一 trace）
```

- 智能体本体无状态；跨 session 记忆全部经 domain-knowledge.md（文件记忆）承载
- 驱动器与智能体分离：驱动器管遍历/断点/预算，智能体管单段校验

## 3. 单次校验工作流（ReAct 循环内）

1. **定位**：读 chunk 正文，graph-read 查 MENTIONED_IN 该 chunk 的实体集
2. **展开**：对核心实体做多跳邻域查询（按需选关系类别，如 OWNS/MEMBER_OF 链）
3. **佐证**：rag.search 相邻主题段落（同章 seq±、同实体他处提及）交叉验证
4. **判定**（五类结论，每实体/关系独立判定）：
   | 判定 | 含义 | 写回动作 |
   |---|---|---|
   | valid | 图谱与原文一致 | record_visit（硬权重） |
   | quality_good/bad | 描述/关系质量好或差 | vote up/down + reason（软权重） |
   | desc_wrong/poor | 描述错误或贫瘠 | revise_description（带证据） |
   | rel_missing | 原文有而图谱缺 | add_relation（带证据+区间） |
   | rel_wrong | 图谱关系错误 | fix_relation / remove_relation |
   | interval_missing | 时序区间缺失 | set_interval |
5. **沉淀**：发现系统性规律（如"战斗章 OWNS 缺失率高"）→ 追加进 domain-knowledge.md 日志节

## 4. 行为渲染（Langfuse）

- **trace = chunk**（trace_id 即 chunk_id，Langfuse UI 按 chunk 聚合全部校验历史）
- **span = 行为**：每次 LLM 推理/工具调用自动成 span（AgentScope OTLP 原生埋点）
- 写操作工具调用携带 trace_id 入图谱 → **图谱任何变更可反查当时完整推理过程**
- 一智能体一 project，成本/延迟按 project 可视

## 5. domain-knowledge.md（文件记忆，可选）

结构固定三节，智能体只读前两节进 system，运行后经工具或驱动器追加第三节：

```markdown
# 校验世界观
## 领域知识（本作特有校验要点, 如: 同名不同人区分、别名高频混淆对）
## 校验策略（优先级: 关系>时序>描述; 抽样深度; 何时停）
## 运行日志（发现/修正的系统性问题, 按日期追加）
```

更新时机：每 session 结束由驱动器把智能体的"心得输出"append 进日志节；领域知识/策略节的人工修订走提示词审批流程（frontmatter approved 机制）。

## 6. 上下文与预算

- system（domain-knowledge 两节 + 任务提示词）+ 当前 chunk + 工具结果，预估 <8k token；AgentScope compress 兜底
- 单 trace 预算：LLM 轮次 ≤10、工具调用 ≤20；超限驱动器截断并标记 incomplete
- 全程预算按 chunk 数线性增长（实测约 95k token/chunk）→ 分批跑，保险丝照旧（连续 3 败冷却 120s 软熔断，累计 20 败硬停）

## 7. 验收

1. 试跑：抽样 chunk（设定类/叙事类/时间线密集型），Langfuse trace + 图谱 diff 人工复核
2. 指标：误写率（人工复核否决的写操作占比）<5%、五类判定分布合理
3. 通过后全量分批运行
