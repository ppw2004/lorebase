# 图谱 Schema

## 节点模型

统一 `Entity` 主标签 + 类型二级标签，`name` 唯一约束。示例取自 examples/ 的自造语料《星屿纪年》。

| 类型 | 标签 | 专属属性 | 例 |
|---|---|---|---|
| person | `Character` | — | 澜、叙霜、乌桑 |
| race | `Race` | — | 潮汐族、岩居族 |
| faction | `Faction` | — | 守灯人公会、深渊教团 |
| location | `Location` | — | 沉钟湾、风暴眼 |
| item | `Item` | — | 潮汐罗盘、沉钟 |
| concept | `Concept` | — | 潮汐术、灯语 |
| event | `Event` | `from`,`to`（时序区间） | 大退潮、风暴眼之战 |

公共属性：`name`、`aliases[]`、`description`（跨块累积）、`source_chunks[]`。
校验智能体追加：`hard_weight`（访问计数）、`soft_weight`（评价净值）、`description_history[]`（修订史）。
`Chunk` 节点：`id`（chunk_id）、`header`（卷·章>节路径）。

## 关系类型（14 种白名单）

MEMBER_OF / LEADER_OF / ENEMY_OF / ALLY_OF / PARENT_OF / TEACHER_OF /
LOCATED_IN / OWNS / PARTICIPATES_IN / CREATED / KILLED / RELATED_TO（兜底）/
INCARNATION_OF（个体→转世/化身概念）/ MASTERS（人物→技能概念）

关系公共属性：`evidence`（原文证据句 ≤60 字）、`source`（chunk_id）、`year`、`freq`（多次抽取=置信信号）。
时序敏感关系（OWNS/MEMBER_OF/LEADER_OF/ALLY_OF/ENEMY_OF 等）追加 `valid_from`/`valid_to`。

## 时序立体化（temporal）

- 时间轴：语料纪年的**整数**年（负数=纪元前；虚拟历法不用 Neo4j date 类型）
- as-of 查询：`WHERE r.valid_from <= $t AND (r.valid_to IS NULL OR r.valid_to > $t)`
- 时代推导不建时间树：`"某某时代"` = 该时期关键个体的生死/活动区间
- 与 RAG 侧呼应：chunk payload `years[]` 做召回过滤，图谱 as-of 做精确回答
- 明确不做：版本化节点 + TIME 链（小说是一次性历史事实，非快照系统；该模式节点爆炸）

## 权重语义（智能体校验）

| 权重 | 来源 | 语义 | 衰减 |
|---|---|---|---|
| `hard_weight` | 智能体访问行为（record_visit） | 事实性/关注度信号 | 不衰减（计数） |
| `soft_weight` | 智能体评价输出（vote up/down + reason） | 质量信号 | 可定期重校 |

所有写操作强制携带 `evidence`（原文）与 `trace_id`（Langfuse），构成"图谱变更可回放"闭环。
