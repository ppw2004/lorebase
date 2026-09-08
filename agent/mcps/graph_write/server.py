"""graph-write MCP：图谱写操作白名单（契约见 docs/mcp-design.md）。

约束：
- 参数化 Cypher + 关系类型白名单，拒绝自由语句入参
- 禁止 import agentscope 或其他 MCP
- 目标不存在返回 {error: not_found}，不自动建节点（防幻觉）
"""
import json
import os
import time

import requests
from fastmcp import FastMCP

NEO4J = os.environ.get("NEO4J_HTTP", "http://localhost:7474")
RELS = {"MEMBER_OF", "LEADER_OF", "ENEMY_OF", "ALLY_OF", "PARENT_OF", "TEACHER_OF",
        "LOCATED_IN", "OWNS", "PARTICIPATES_IN", "CREATED", "KILLED", "RELATED_TO",
        "INCARNATION_OF", "MASTERS"}
HIST_MAX = 20

mcp = FastMCP("graph-write")


def _pw():
    pw = os.environ.get("NEO4J_PASSWORD")
    if not pw:
        raise RuntimeError("NEO4J_PASSWORD not set (see .env.example)")
    return pw


def cypher(stmt, params):
    r = requests.post(f"{NEO4J}/db/neo4j/query/v2",
                      json={"statement": stmt, "parameters": params},
                      auth=("neo4j", _pw()), timeout=30)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"].get("values", [])


def _ok(**kw):
    return {"ok": True, **kw}


def _not_found(kind, key):
    return {"error": "not_found", "hint": f"{kind} '{key}' 不存在; 不自动创建, 请先核实原文"}


# ---- node 工具 ----

def _match_node(name, set_clause, params):
    rows = cypher(f"MATCH (e:Entity {{name: $name}}) {set_clause} RETURN e.name, "
                  f"e.hard_weight, e.soft_weight", params)
    return rows[0] if rows else None


@mcp.tool
def record_visit(node_name: str, trace_id: str = "") -> dict:
    """访问节点: hard_weight +1 (硬权重, 智能体访问行为)."""
    row = _match_node(node_name,
        "SET e.hard_weight = coalesce(e.hard_weight, 0) + 1, "
        "e.last_visit_ts = timestamp(), e.last_trace = $trace",
        {"name": node_name, "trace": trace_id})
    if not row:
        return _not_found("node", node_name)
    return _ok(node=node_name, hard_weight=row[1])


@mcp.tool
def vote(kind: str, up: bool, reason: str, node_name: str = "",
         head: str = "", rel: str = "", tail: str = "",
         trace_id: str = "") -> dict:
    """评价节点或关系: soft_weight ±1 (软权重); reason 必填. kind: node|rel."""
    if not reason.strip():
        return {"error": "bad_request", "hint": "reason 必填"}
    delta = 1 if up else -1
    if kind == "node":
        entry = json.dumps({"up": up, "reason": reason[:200],
                            "at": int(time.time() * 1000), "trace": trace_id}, ensure_ascii=False)
        rows = cypher("MATCH (e:Entity {name: $n}) "
                      "SET e.soft_weight = coalesce(e.soft_weight, 0) + $d "
                      "SET e.vote_log = (coalesce(e.vote_log, []) + [$entry])[0..$h] "
                      "RETURN e.soft_weight",
                      {"n": node_name, "d": delta, "entry": entry, "h": HIST_MAX})
    elif kind == "rel" and rel in RELS:
        entry = json.dumps({"up": up, "reason": reason[:200],
                            "at": int(time.time() * 1000), "trace": trace_id}, ensure_ascii=False)
        rows = cypher(f"MATCH (a:Entity {{name: $hn}})-[r:{rel}]->(b:Entity {{name: $tn}}) "
                      "SET r.soft_weight = coalesce(r.soft_weight, 0) + $d "
                      "SET r.vote_log = (coalesce(r.vote_log, []) + [$entry])[0..$h] "
                      "RETURN r.soft_weight",
                      {"hn": head, "tn": tail, "d": delta, "entry": entry, "h": HIST_MAX})
    else:
        return {"error": "bad_request", "hint": "kind=node|rel, rel 须在白名单"}
    if not rows:
        return _not_found(kind, f"{head}-[{rel}]->{tail}" if kind == "rel" else node_name)
    return _ok(soft_weight=rows[0][0])


@mcp.tool
def revise_description(new_text: str, evidence: str, node_name: str = "",
                       kind: str = "node", head: str = "", rel: str = "", tail: str = "",
                       trace_id: str = "") -> dict:
    """修订关键描述: 覆盖 description, 旧值入 description_history (可回溯 trace)."""
    if kind == "node":
        olds = cypher("MATCH (e:Entity {name: $n}) RETURN e.description", {"n": node_name})
        if not olds:
            return _not_found("node", node_name)
        entry = json.dumps({"by": "agent", "at": int(time.time() * 1000),
                            "trace": trace_id, "old": olds[0][0]}, ensure_ascii=False)
        rows = cypher("MATCH (e:Entity {name: $n}) "
                      "SET e.description_history = (coalesce(e.description_history, []) + [$entry])[0..$h] "
                      "SET e.description = $new, e.desc_evidence = $ev "
                      "RETURN e.name",
                      {"n": node_name, "new": new_text[:500], "ev": evidence[:120],
                       "entry": entry, "h": HIST_MAX})
    elif kind == "rel" and rel in RELS:
        olds = cypher(f"MATCH (a:Entity {{name: $h}})-[r:{rel}]->(b:Entity {{name: $t}}) "
                      "RETURN r.description",
                      {"h": head, "t": tail})
        if not olds:
            return _not_found("rel", f"{head}-[{rel}]->{tail}")
        entry = json.dumps({"by": "agent", "at": int(time.time() * 1000),
                            "trace": trace_id, "old": olds[0][0]}, ensure_ascii=False)
        rows = cypher(f"MATCH (a:Entity {{name: $h}})-[r:{rel}]->(b:Entity {{name: $t}}) "
                      "SET r.description_history = (coalesce(r.description_history, []) + [$entry])[0..$h] "
                      "SET r.description = $new, r.desc_evidence = $ev "
                      "RETURN a.name",
                      {"h": head, "t": tail, "new": new_text[:500], "ev": evidence[:120],
                       "entry": entry, "h": HIST_MAX})
    else:
        return {"error": "bad_request", "hint": "kind=node|rel, rel 须在白名单"}
    if not rows:
        return _not_found(kind, f"{head}-[{rel}]->{tail}" if kind == "rel" else node_name)
    return _ok(revised=rows[0][0])


@mcp.tool
def set_interval(valid_from: int | None, valid_to: int | None, evidence: str,
                 node_name: str = "", kind: str = "node", head: str = "",
                 rel: str = "", tail: str = "", trace_id: str = "") -> dict:
    """时序立体化: 补全/修正区间 (纪元年整数, 纪元前为负; None 保持不变)."""
    sets, params = [], {"ev": evidence[:120], "t": trace_id}
    if valid_from is not None:
        sets.append("T.valid_from = $vf")
        params["vf"] = valid_from
    if valid_to is not None:
        sets.append("T.valid_to = $vt")
        params["vt"] = valid_to
    if not sets:
        return {"error": "bad_request", "hint": "valid_from/valid_to 至少一个"}
    set_clause = "SET " + ", ".join(sets) + ", T.interval_evidence = $ev, T.interval_trace = $t"
    if kind == "node":
        rows = cypher("MATCH (T:Entity {name: $n}) " + set_clause + " RETURN T.name",
                      {**params, "n": node_name})
        key = node_name
    elif kind == "rel" and rel in RELS:
        rows = cypher(f"MATCH (a:Entity {{name: $h}})-[r:{rel}]->(b:Entity {{name: $t2}}) "
                      + set_clause.replace("T.", "r.") + " RETURN a.name",
                      {**params, "h": head, "t2": tail})
        key = f"{head}-[{rel}]->{tail}"
    else:
        return {"error": "bad_request", "hint": "kind=node|rel, rel 须在白名单"}
    if not rows:
        return _not_found(kind, key)
    return _ok(target=key)


@mcp.tool
def add_relation(head: str, rel: str, tail: str, evidence: str,
                 valid_from: int | None = None, valid_to: int | None = None,
                 trace_id: str = "") -> dict:
    """新增关系 (rel 限 14 白名单); 头尾实体必须已存在, 幂等 MERGE."""
    if rel not in RELS:
        return {"error": "bad_request", "hint": f"rel 须在白名单: {sorted(RELS)}"}
    rows = cypher(
        f"MATCH (h:Entity {{name: $a}}), (t:Entity {{name: $b}}) "
        f"OPTIONAL MATCH (h)-[existing:{rel}]->(t) "
        f"MERGE (h)-[r:{rel}]->(t) "
        "ON CREATE SET r.evidence = $ev, r.source = 'agent', r.trace = $t, r.freq = 1, "
        "r.valid_from = $vf, r.valid_to = $vt "
        "ON MATCH SET r.freq = coalesce(r.freq, 0) + 1 "
        "RETURN h.name, t.name, r.freq",
        {"a": head, "b": tail, "ev": evidence[:120], "t": trace_id,
         "vf": valid_from, "vt": valid_to})
    if not rows:
        missing = cypher("MATCH (n:Entity) WHERE n.name IN [$a, $b] RETURN n.name",
                         {"a": head, "b": tail})
        have = {r[0] for r in missing}
        return _not_found("entity", f"{head if head not in have else tail}")
    return _ok(head=head, rel=rel, tail=tail, freq=rows[0][2])


@mcp.tool
def fix_relation(head: str, rel: str, tail: str, evidence: str,
                 year: int | None = None, valid_from: int | None = None,
                 valid_to: int | None = None, trace_id: str = "") -> dict:
    """修正既有关系的证据/年份/区间 (仅更新显式给出的字段)."""
    if rel not in RELS:
        return {"error": "bad_request", "hint": "rel 须在白名单"}
    sets = ["r.evidence = $ev", "r.fix_trace = $t"]
    params = {"h": head, "t2": tail, "ev": evidence[:120], "t": trace_id}
    if year is not None:
        sets.append("r.year = $y")
        params["y"] = year
    if valid_from is not None:
        sets.append("r.valid_from = $vf")
        params["vf"] = valid_from
    if valid_to is not None:
        sets.append("r.valid_to = $vt")
        params["vt"] = valid_to
    rows = cypher(f"MATCH (a:Entity {{name: $h}})-[r:{rel}]->(b:Entity {{name: $t2}}) "
                  f"SET {', '.join(sets)} RETURN a.name", params)
    if not rows:
        return _not_found("rel", f"{head}-[{rel}]->{tail}")
    return _ok(fixed=f"{head}-[{rel}]->{tail}")


@mcp.tool
def remove_relation(head: str, rel: str, tail: str, reason: str, evidence: str,
                    trace_id: str = "") -> dict:
    """删除错误关系 (需 reason + evidence, 关系快照序列化入 removal_log)."""
    if rel not in RELS:
        return {"error": "bad_request", "hint": "rel 须在白名单"}
    olds = cypher(f"MATCH (a:Entity {{name: $h}})-[r:{rel}]->(b:Entity {{name: $t2}}) "
                  "RETURN properties(r) AS p",
                  {"h": head, "t2": tail})
    if not olds:
        return _not_found("rel", f"{head}-[{rel}]->{tail}")
    entry = json.dumps({"rel": rel, "tail": tail, "reason": reason[:200],
                        "evidence": evidence[:120], "at": int(time.time() * 1000),
                        "trace": trace_id, "removed_props": olds[0][0]}, ensure_ascii=False)
    rows = cypher(
        f"MATCH (a:Entity {{name: $h}})-[r:{rel}]->(b:Entity {{name: $t2}}) "
        "WITH a, b, r DELETE r WITH a "
        "SET a.removal_log = (coalesce(a.removal_log, []) + [$entry])[0..$hmax] "
        "RETURN a.name",
        {"h": head, "t2": tail, "entry": entry, "hmax": HIST_MAX})
    if not rows:
        return _not_found("rel", f"{head}-[{rel}]->{tail}")
    return _ok(removed=f"{head}-[{rel}]->{tail}")


if __name__ == "__main__":
    mcp.run()
