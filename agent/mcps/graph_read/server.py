"""graph-read MCP：图谱只读查询（契约见 docs/mcp-design.md）。

官方 neo4j-mcp-server 1.5.3 硬依赖 APOC(meta) 插件, 本集群 CE 未装,
故自写只读薄层(选型变更记录见 docs/decisions/)。
"""
import os
import re

import requests
from fastmcp import FastMCP

NEO4J = os.environ.get("NEO4J_HTTP", "http://localhost:7474")
WRITE_RE = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD\s+CSV|"
                      r"FOREACH|BEGIN|COMMIT|CALL\s+(?!db\.))\b", re.I)

mcp = FastMCP("graph-read")


def _pw():
    pw = os.environ.get("NEO4J_PASSWORD")
    if not pw:
        raise RuntimeError("NEO4J_PASSWORD not set (see .env.example)")
    return pw


def cypher(stmt, params=None):
    r = requests.post(f"{NEO4J}/db/neo4j/query/v2",
                      json={"statement": stmt, "parameters": params or {}},
                      auth=("neo4j", _pw()), timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"].get("values", [])


@mcp.tool
def get_schema() -> dict:
    """图结构概览: 标签/关系类型/属性键/统计。"""
    return {
        "labels": [r[0] for r in cypher("CALL db.labels()")],
        "rel_types": [r[0] for r in cypher("CALL db.relationshipTypes()")],
        "prop_keys": [r[0] for r in cypher("CALL db.propertyKeys()")],
        "counts": {"entities": cypher("MATCH (e:Entity) RETURN count(e)")[0][0],
                   "chunks": cypher("MATCH (c:Chunk) RETURN count(c)")[0][0],
                   "relations": cypher("MATCH ()-[r]->() RETURN count(r)")[0][0]},
    }


@mcp.tool
def read_cypher(query: str, limit: int = 50) -> dict:
    """只读 Cypher(写语句拦截); 自动加 RETURN 行数上限保护。"""
    if WRITE_RE.search(query):
        return {"error": "forbidden", "hint": "只读 MCP, 写操作请用 graph-write 工具"}
    try:
        rows = cypher(query)
        return {"rows": rows[:limit], "n": min(len(rows), limit), "truncated": len(rows) > limit}
    except Exception as e:
        return {"error": "cypher_error", "hint": str(e)[:200]}


@mcp.tool
def neighbors(name: str, rel: str = "", hops: int = 1, limit: int = 30) -> dict:
    """实体多跳邻域(默认1跳, 最多3跳)。rel 留空=全部关系类型。"""
    if rel and not re.fullmatch(r"[A-Z_]+", rel):
        return {"error": "bad_request", "hint": "rel 须大写字母关系类型"}
    depth = max(1, min(hops, 3))
    pat = f":{rel}" if rel else ""
    rows = cypher(
        f"MATCH p = (a:Entity {{name: $n}})-[{pat}*1..{depth}]-(b:Entity) "
        "WITH a, b, [t IN relationships(p) | type(t)] AS rels, length(p) AS d "
        "RETURN a.name, b.name, rels, d ORDER BY d LIMIT $lim",
        {"n": name, "lim": limit})
    return {"center": name,
            "neighbors": [{"name": r[1], "rels": r[2], "hops": r[3]} for r in rows]}


@mcp.tool
def chunk_entities(chunk_id: str) -> dict:
    """该切片块(或单元)提及的全部实体(校验定位用)。"""
    rows = cypher(
        "MATCH (ch:Chunk) WHERE ch.id = $id OR $id IN coalesce(ch.chunk_ids, []) "
        "MATCH (e:Entity)-[:MENTIONED_IN]->(ch) "
        "RETURN e.name, e.type, e.description ORDER BY e.name",
        {"id": chunk_id})
    return {"chunk_id": chunk_id,
            "entities": [{"name": r[0], "type": r[1], "desc": (r[2] or "")[:120]} for r in rows]}


if __name__ == "__main__":
    mcp.run()
