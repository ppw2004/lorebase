# agent/ 智能体层

```
agent/
├── agent.py          # AgentScope 入口（build_agent / build_toolkit / run_chunk）
├── runner.py         # 校验驱动器（遍历 chunk, 断点续跑, 熔断, Langfuse trace）
├── domain-knowledge.example.md  # 文件记忆模板（复制为 domain-knowledge.md, gitignored）
├── mcps/
│   ├── graph_read/   # 自写 FastMCP 只读薄层（get_schema/read_cypher/neighbors/chunk_entities）
│   ├── graph_write/  # 自写 FastMCP：权重/描述/关系/时序白名单操作
│   └── rag/          # 自写 FastMCP：dashscope 向量召回 + payload 过滤
└── web/              # AG-UI 流式问答服务（server.py + static/）
```

- 智能体不直连任何数据库，一切经 MCP；提示词放 docs/prompts/（代码零提示词字面量，frontmatter 未 approved 拒载）
- 双 venv：`.venv`（agentscope[service] 主环境）与 `.venv-mcp`（fastmcp，与 agentscope 的 mcp pin 冲突故分离）
- 快速上手见仓库 README 的「校验」与「问答服务」两节
