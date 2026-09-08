# 总体架构

任意小说/叙事文本 → 向量 + 图谱双库 → MCP 智能体校验 + AG-UI 问答。

## 全景

```
语料 (corpus/*.md, 章节结构化 markdown)
   │
   ▼  scripts/01~04 数据管线
   ├── 01_chunking      结构化切片（章/节边界, ~500字, 尾段重叠, 块头上下文, years）
   ├── 02/02b           实体词典（LLM 抽取 + 扇出清洗 + LLM 语义归并）
   ├── 03               图谱抽取（mentions/triples + gleaning）→ Neo4j
   └── 04               embedding（dashscope qwen3.7, 1024 维）→ Qdrant lorebase_chunks
   │
   ▼  双库（可自托管, 默认 localhost）
   ├── Qdrant            语义检索面（chunk 向量 + 实体向量 kind 标记）
   └── Neo4j             结构推理面（Entity 多标签图 + Chunk 回链 + 时序区间）
   │
   ▼  agent/ 智能体（AgentScope 2.x）
   ├── mcp:graph-read    自写只读薄层（read_cypher / neighbors / chunk_entities）
   ├── mcp:graph-write   自写薄 MCP（权重/描述/关系/时序白名单操作）
   ├── mcp:rag           自写薄 MCP（dashscope embedding + payload 过滤召回）
   ├── validator         逐 chunk 校验图谱（访问=硬权重、评价=软权重、修订带证据）
   └── web/              AG-UI 流式问答服务（只读双 MCP）
   └── observability     OTLP → Langfuse（一智能体一 project; 一段落一 trace）
```

## 核心设计决策（依据见 decisions/）

1. **双库分工**：Qdrant 管"找到相关原文"，Neo4j 管"结构化推理与关系遍历"；`MENTIONED_IN` 边 + chunk_id 双向互联
2. **切块头上下文进向量**（text_ctx）：世界观语料脱离章节语境不可解读，参考 LightRAG section breadcrumb
3. **词典先行**：别名/马甲归并是成图质量的第一变量（同一个人的本名/称号/绰号必须归并到同一实体）
4. **时序立体化**：区间属性（valid_from/to，纪元年整数）+ 事件中心，不做版本化节点链（小说非快照系统，无节点爆炸风险）
5. **智能体校验闭环**：访问=硬权重、评价=软权重、修订带证据与 trace 回链——图谱每次变更可在 Langfuse 回放

## 环境

- 运行：Python 3.11+（管线仅依赖 requests；智能体需 venv 装 agentscope）
- LLM：MiniMax-M3（抽取/校验主力，OpenAI 兼容口可换任意模型）、dashscope（embedding）
- 密钥：全部走环境变量（见 .env.example），不入仓库
- 成本护栏：用量保险丝 + `build/usage_log.jsonl` 账本（百万字级小说全管线实测为千万 token 量级）
