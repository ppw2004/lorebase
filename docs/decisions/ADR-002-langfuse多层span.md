# ADR-002: Langfuse 多层 span 埋点架构（踩坑全记录）

背景: 校验智能体需要"一 chunk 一 trace 树"（agent→model 轮次→MCP 工具调用）的可观测性。
决策: reply/model 层用 AgentScope 原生 TracingMiddleware；tool 层自定义 on_acting 用 **langfuse SDK 的 `start_as_current_observation(as_type='tool')`（with 形式）**。

## 踩坑清单（按时间序，勿再踩）

1. **SDK 2.x legacy ingestion 已被 v4 服务端移除**（400）——必须 4.x SDK + OTLP
2. **langfuse 4.24 服务端 OTLP 摄取有 CH DateTime64 bug**（span 全丢）→ 服务端升级 4.28.1 修复（用户完成）；worker 日志特征 `Numeric value is out of range for DateTime64`
3. **AgentScope `TracingMiddleware.on_acting`（框架版）与 MCP stdio 的 anyio cancel scope 跨 task 冲突**——每次工具调用 RuntimeError → 必须自定义 on_acting
4. **裸 OTel `tracer.start_span()` 建 tool span 无 parent** → 孤立 trace；补 `context=otel_context.get_current()` 可挂 parent
5. **裸 OTel span 设 `langfuse.observation.type='tool'` 属性服务端不认**（A/B 实验：SDK 原生 → TOOL 落库；裸 span+属性 → 普通SPAN 或被丢）——**必须走 SDK API**
6. `start_as_current_observation` 不加 with 返回 `_AgnosticContextManager`，无 `.update()/.end()`——必须 `with ... as obs:` 形式
7. **时序铁律**: `Langfuse()` 实例化必须先于 Agent 创建（它注册全局 OTel provider，agentscope 的 `_check_tracing_enabled()` 检查全局 provider 是否 SDK 类型）
8. MCP 客户端**全程复用**（逐 chunk 新建+close 会污染 anyio cancel scope，后续 chunk 子进程启动被取消）
9. `Msg(name=..., role=..., content=[{"type":"text","text":...}])`——content 必须块列表
10. 权限: 无人值守用 `PermissionMode.BYPASS`（DONT_ASK = 全拒）；写防护在 MCP 白名单层
11. 已知无害噪音: 进程收尾时 3 个 MCP close 各报一次 cancel scope RuntimeError

## 最终 span 树形态（单 chunk ~33 span）

```
CHAIN validate:{chunk_id}   ← runner 建的根(input=chunk_id, metadata=usage)
└─ AGENT invoke_agent lorebase-validator
   ├─ GENERATION chat MiniMax-M3  ×11   ← 每轮推理(含工具请求)
   └─ TOOL tool:mcp__*__*  ×N           ← 每次 MCP 调用(input/output 全量)
```
UI 中 chat span 的"not called"工具 = 该轮携带的工具定义清单中未被调用的，正常现象。
