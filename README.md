# Lorebase

小说/叙事文本 → 向量 + 图谱双知识库 → MCP 智能体校验 → AG-UI 流式问答。

一个把"读完一套书"变成"可检索、可推理、可对话的世界观知识库"的完整流水线：

```
你的语料(markdown)                     你的 LLM/向量/图数据库
     │                                      │
 01 切片 ──► 02/02b 实体词典 ──► 03 图谱 ──► Neo4j ──┐
     │              │                                ├─► graph-read ─┐
     └──── 04 向量 ───────────────► Qdrant ──► rag ──┤               ├─► 校验智能体(写回权重/修订/时序)
                                                     └─ graph-write ─┘
                                                                          AG-UI 问答服务(只读)
```

## 特性

- **结构化切片**：章/节边界 + ~500 字贪心聚合 + 尾段重叠 + 块头上下文进向量（世界观文本脱离章节语境不可解读）
- **词典先行**：LLM 抽取 + 扇出清洗（泛称词剔除）+ LLM 语义归并——别名归并质量是成图质量的第一变量
- **双库分工**：Qdrant 找相关原文，Neo4j 做关系遍历与 as-of 时序查询；`MENTIONED_IN` 边 + chunk_id 双向互联
- **MCP 工具层**：graph-read（只读薄层，写语句拦截）/ graph-write（参数化 Cypher + 14 关系白名单，防 LLM 裸拼）/ rag（向量召回 + payload 过滤）
- **智能体校验闭环**：逐 chunk 比对"原文 ↔ 图谱子图 ↔ 向量召回"，访问=硬权重、评价=软权重、修订必带原文证据与 Langfuse trace 回链——图谱每次变更可回放
- **AG-UI 流式问答**：思考、工具调用、正文按真实时序流式可见（AgentScope 原生事件流 → SSE）

## 快速开始

前置：Python 3.11+、Neo4j 5.x、Qdrant 1.x、MiniMax 与 DashScope API key（`.env.example` 有全部变量）。

```bash
# 1) 管线：examples/ 自带 1600 字自造语料，可零成本跑通全链路
cp .env.example .env && vi .env  # 填密钥, source .env
mkdir -p corpus && cp examples/corpus/star-isles.md corpus/
export LOREBASE_SKIP_TITLES=星屿纪年  # 书名标题跳过(按你的语料改)
python3 scripts/01_chunking.py
python3 scripts/02_entity_dict.py && python3 scripts/02b_merge_dict.py
python3 scripts/03_extract_graph.py extract && python3 scripts/03_extract_graph.py load
cp examples/golden_queries.tsv build/golden_queries.tsv
python3 scripts/03_extract_graph.py verify   # 金查询应全 PASS
python3 scripts/04_embed_qdrant.py

# 2) 换成语料: epub 先转 markdown, 多卷放 corpus/ 按文件名排序
python3 scripts/convert_epub_to_md.py book.epub corpus/vol1.md

# 3) 校验智能体(可选, 写回权重/修订)
python3 -m venv agent/.venv && agent/.venv/bin/pip install -r agent/web/requirements-main.txt
python3 -m venv agent/.venv-mcp && agent/.venv-mcp/bin/pip install -r agent/web/requirements-mcp.txt
vi docs/prompts/node-validation.prompt.md   # 按语料改写, frontmatter 置 approved(未审批拒载)
agent/.venv/bin/python agent/runner.py --sample 3

# 4) 问答服务(只读双 MCP)
vi docs/prompts/qa.prompt.md                # 同上置 approved
agent/.venv/bin/python -m uvicorn agent.web.server:app --port 8300
# 或 docker build -t lorebase-qa . && docker run -p 8300:8300 --env-file .env lorebase-qa
```

详细设计见 [docs/](docs/)（架构 / 图谱 schema / MCP 契约 / 智能体设计 / ADR）。

## 语料与合规

- 本仓**不随附任何真实作品语料**；examples/ 为自造示例，全部专有名词均为虚构
- 用自己的语料建库时请确保拥有相应权利；产出的知识图谱与检索服务仅限个人研究用途
- 本项目与任何小说/出版方无关，不为第三方基于本框架构建的内容背书

## License

Apache-2.0
