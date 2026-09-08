# MCP 设计契约

## 选型结论（调研依据见 git 历史）

| MCP | 实现 | 理由 |
|---|---|---|
| graph-read | **自写只读薄层**（原定官方包；变更见 ADR-001） | 官方 1.5.3 get-schema 硬依赖 APOC(CE 未装), 不为插件动共享数据库；自写含 get_schema/read_cypher(写拦截)/neighbors(多跳)/chunk_entities(块级反查) |
| graph-write | **自写**（FastMCP 薄层） | 官方 write-cypher 让 LLM 裸拼 Cypher 不可接受；业务语义白名单操作 |
| rag | **自写**（FastMCP 薄层） | Qdrant 官方 MCP 是 fastembed 本地记忆设计，不匹配 dashscope 1024 维 + payload 过滤需求 |

统一约束：stdio transport（AgentScope stateful 模式）；graph-write 所有工具参数化 Cypher，拒绝自由语句入参。

## 工具契约

### graph-write（自写）

| 工具 | 签名 | 语义 |
|---|---|---|
| record_visit | `(node_name)` | hard_weight +1，记 trace_id |
| vote | `(target, kind: node\|rel, up\|down, reason)` | soft_weight ±1，reason 必填 |
| revise_description | `(target, kind, new_text, evidence, trace_id)` | 覆盖 description，追加 description_history[{by,at,trace,text_old}] |
| set_interval | `(target, kind, from?, to?, evidence)` | 时序区间补全/修正 |
| add_relation | `(head, rel, tail, from?, to?, evidence)` | rel ∈ 14 白名单；MERGE 幂等 |
| fix_relation | `(head, rel, tail, changes{}, evidence)` | 修正 evidence/year/interval；删关系单列 `remove_relation(reason, evidence)` |

错误约定：`{error, hint}` 结构化返回；目标不存在 → error=not_found（不自动建节点，防幻觉）。

### rag（自写）

| 工具 | 签名 | 语义 |
|---|---|---|
| search | `(query, kind?, years_min?, years_max?, volume?, limit=8)` | qwen3.7 向量召回，payload 过滤，返回 chunk_id/text_ctx/块头/score |
| read_chunk | `(chunk_id)` | 整块正文（含相邻块 seq±1，供上下文扩展） |

### graph-read（自写，只读）

get_schema / read_cypher（正则写拦截）/ neighbors（多跳）/ chunk_entities（块级反查）。
连接：`NEO4J_HTTP` 环境变量（默认 `http://localhost:7474`）。

## 部署

- 三 MCP 均独立进程部署（与数据库同机时延最低）；密钥走环境变量
- AgentScope 侧：`Toolkit(mcps=[...])`，`mcp__{server}__{tool}` 命名空间；readOnlyHint 标注读类工具
