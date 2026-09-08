# ADR-001: graph-read 弃官方包改自写

背景: 官方 neo4j-mcp-server 1.5.3 (PyPI) 的 get-schema 硬依赖 APOC(meta) 插件, 自托管 Neo4j 5.26 CE 未装。
决策: graph-read 自写只读薄 MCP(写语句正则拦截 + 智能体专用工具 neighbors/chunk_entities), 不为此修改共享数据库部署。
理由: 装 APOC 需动共享 Neo4j (重启/插件下载), 影响面大; 自写 30 行与 graph-write 对称, 零外部依赖。
后果: 失去官方 read-cypher 的 EXPLAIN 级只读强制(自写为关键字拦截, 对自用智能体足够); 后续若升级 Neo4j 或装 APOC 可切回官方包。
