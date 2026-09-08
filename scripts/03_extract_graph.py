#!/usr/bin/env python3
"""阶段3: 图谱抽取与 Neo4j 入库。

用法:
  python3 03_extract_graph.py extract   # 逐单元 M3 抽取(词典候选+gleaning), 产物 build/graph_raw/*.json
  python3 03_extract_graph.py load      # 全部结果经 Neo4j HTTP API MERGE 入图
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module

chunking = import_module("01_chunking")
dict_mod = import_module("02_entity_dict")

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "build" / "graph_raw"
DICT = ROOT / "build" / "entity_dict.json"
NEO4J = os.environ.get("NEO4J_HTTP", "http://localhost:7474")
CONCURRENCY = 4

# Neo4j 二级标签与边类型白名单(防 Cypher 注入)
LABELS = {"person": "Character", "race": "Race", "faction": "Faction", "location": "Location",
          "item": "Item", "concept": "Concept", "event": "Event"}
RELS = ["MEMBER_OF", "LEADER_OF", "ENEMY_OF", "ALLY_OF", "PARENT_OF", "TEACHER_OF",
        "LOCATED_IN", "OWNS", "PARTICIPATES_IN", "CREATED", "KILLED", "RELATED_TO",
        "INCARNATION_OF", "MASTERS"]
REL_HINT = "\n".join(
    f"- {r}" for r in
    ["MEMBER_OF(人物→组织/种族/势力, 成员或隶属)", "LEADER_OF(→组织, 领导)", "ENEMY_OF(↔敌对)",
     "ALLY_OF(↔同盟合作)", "PARENT_OF(→人物, 长辈对晚辈)", "TEACHER_OF(→人物, 师承)",
     "LOCATED_IN(→地点, 位于/属于)", "OWNS(→物品, 持有)", "PARTICIPATES_IN(→事件, 参与)",
     "CREATED(→组织/物品/事件, 创造建立)", "KILLED(→人物, 击杀)",
     "INCARNATION_OF(→概念, 个体是某概念的转世/化身)",
     "MASTERS(→概念, 掌握技能/技艺)",
     "RELATED_TO(以上都不合适时)"]
)

PROMPT = """你是小说世界观知识库的构建助手。基于给定文本和候选实体清单，抽取实体提及与关系三元组。

候选实体清单(格式 主名[别名]: 描述)：
{dict_part}

任务：
1. mentions: 文本中实际出现的实体, 使用清单中的规范主名; 清单外的重要新实体也可输出(is_new=true)
2. triples: 实体间关系, 必须选关系类型: 
{rel_hint}
三元组 head/tail 用规范主名, 给出证据短句(原文≤60字), 有纪元年份就填 year(整数, 纪元前为负)

只输出 JSON 对象: {{"mentions": [{{"name": "", "type": "person|race|faction|location|item|concept|event", "is_new": false, "evidence": ""}}], "triples": [{{"head": "", "rel": "", "tail": "", "evidence": "", "year": null}}]}}

文本：
"""

GLEAN_PROMPT = """刚才你从文本抽取了以下实体和三元组(JSON):
{first}

请再仔细读一遍文本，补充遗漏的实体提及(mentions)和关系(triples)，尤其注意：别名/代称指回主名、隐含的师徒亲属敌我关系、事件参与。没有遗漏就输出空数组。格式同前，只输出 JSON 对象: {{"mentions": [], "triples": []}}

文本：
"""


def build_dict_index(entity_dict):
    names = {}
    for e in entity_dict:
        for n in [e["canonical"]] + e["aliases"]:
            if len(n) >= 2:
                names[n] = e["canonical"]
    return names


def candidates(text, names):
    found = set()
    for alias, canonical in names.items():
        if alias in text:
            found.add(canonical)
    return found


def extract_unit(unit_text, entity_dict, names):
    cands = candidates(unit_text, names)
    dict_part = "\n".join(
        f"{e['canonical']}[{','.join(e['aliases'][:3])}]: {e['desc'][:40]}"
        for e in entity_dict if e["canonical"] in cands
    )[:8000]
    p1 = PROMPT.format(dict_part=dict_part or "(无)", rel_hint=REL_HINT) + unit_text
    first = dict_mod.call_m3(p1, purpose="extract")
    if isinstance(first, list):  # M3 偶发输出数组而非对象: 解释为 mentions
        first = {"mentions": first, "triples": []}
    p2 = GLEAN_PROMPT.format(first=json.dumps(first, ensure_ascii=False)[:6000]) + unit_text
    try:
        extra = dict_mod.call_m3(p2, purpose="glean")
        if isinstance(extra, list):
            extra = {"mentions": extra, "triples": []}
    except Exception:
        extra = {"mentions": [], "triples": []}
    mentions = first.get("mentions", []) + extra.get("mentions", [])
    triples = first.get("triples", []) + extra.get("triples", [])
    return {"mentions": mentions, "triples": triples}


def norm_name(s):
    return re.sub(r"\s+", "", str(s or ""))[:40]


def sanitize(u):
    ms = []
    seen = set()
    for m in u.get("mentions", []):
        n = norm_name(m.get("name"))
        if not n or n in seen:
            continue
        seen.add(n)
        ms.append({"name": n, "type": m.get("type") if m.get("type") in LABELS else "concept",
                   "is_new": bool(m.get("is_new")), "evidence": str(m.get("evidence", ""))[:120]})
    ts = []
    for t in u.get("triples", []):
        h, r, tl = norm_name(t.get("head")), str(t.get("rel", "")).upper().strip(), norm_name(t.get("tail"))
        r = r if r in RELS else "RELATED_TO"
        if not h or not tl or h == tl:
            continue
        y = t.get("year")
        ts.append({"head": h, "rel": r, "tail": tl, "evidence": str(t.get("evidence", ""))[:120],
                   "year": int(y) if isinstance(y, (int, float)) else None})
    return {"mentions": ms, "triples": ts}


def run_extract():
    dict_mod.check_quota()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    entity_dict = json.loads(DICT.read_text(encoding="utf-8"))
    names = build_dict_index(entity_dict)
    units = [(uid, header, text) for vol, uid, header_text, _, _ in dict_mod.iter_units()
             for header, text in [(header_text.split("\n")[0], header_text)]]

    def work(uid, header, text):
        out = RAW_DIR / f"{uid}.json"
        if out.exists():
            return uid, "cached", 0
        raw = extract_unit(text, entity_dict, names)
        clean = sanitize(raw)
        clean["unit"] = {"uid": uid, "chapter": header}
        out.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        return uid, "ok", len(clean["triples"])

    t0, done, fail = time.time(), 0, 0
    with ThreadPoolExecutor(CONCURRENCY) as ex:
        futs = [ex.submit(work, u[0], u[1], u[2]) for u in units]
        for f in as_completed(futs):
            try:
                uid, status, n = f.result()
                done += 1
                print(f"[{done}/{len(units)}] {uid} {status} triples={n} {time.time()-t0:.0f}s", flush=True)
            except dict_mod.FuseBlown as e:
                fail += 1
                print(f"FUSE BLOWN: {e} — aborting, progress kept for resume", flush=True)
                for f2 in futs:
                    f2.cancel()
                break
            except Exception as e:
                fail += 1
                print(f"FAIL({fail}): {type(e).__name__}: {e}", flush=True)
    print(f"extract done ok={done-fail} fail={fail}")


def load_key(var):
    v = os.environ.get(var)
    if not v:
        raise RuntimeError(f"{var} not set (see .env.example)")
    return v


def cypher(stmt, params=None):
    r = requests.post(f"{NEO4J}/db/neo4j/query/v2",
                      json={"statement": stmt, "parameters": params or {}},
                      auth=("neo4j", load_key("NEO4J_PASSWORD")),
                      timeout=60)
    r.raise_for_status()
    body = r.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return body["data"].get("values", [])


def run_load():
    cypher("CREATE CONSTRAINT ent_name IF NOT EXISTS FOR (e:Entity) REQUIRE e.name IS UNIQUE")
    cypher("CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE")
    label_of = LABELS
    files = sorted(RAW_DIR.glob("*.json"))
    ent_rows, tri_rows = [], []
    for fp in files:
        u = json.loads(fp.read_text(encoding="utf-8"))
        cid = fp.stem
        header = u["unit"]["chapter"]
        ent_rows.append({"id": cid, "header": header,
                         "mentions": [[m["name"], m["type"], m["evidence"]] for m in u["mentions"]]})
        for t in u["triples"]:
            tri_rows.append([t["head"], t["rel"], t["tail"], t["evidence"], cid, t["year"]])

    cypher("UNWIND $rows AS r MERGE (c:Chunk {id: r.id}) SET c.header = r.header "
           "WITH r, c UNWIND r.mentions AS m MERGE (e:Entity {name: m[0]}) "
           "ON CREATE SET e.type = m[1], e.aliases = [], e.description = m[2] "
           "ON MATCH SET e.type = coalesce(e.type, m[1]), "
           "e.description = left(CASE WHEN coalesce(e.description,'') CONTAINS m[2] THEN e.description ELSE coalesce(e.description,'') + '\\n' + m[2] END, 2000) "
           "MERGE (e)-[:MENTIONED_IN]->(c)", {"rows": ent_rows})

    for type_cn, label in LABELS.items():
        cypher(f"MATCH (e:Entity) WHERE e.type = $t SET e:{label}", {"t": type_cn})

    by_rel = {}
    for row in tri_rows:
        by_rel.setdefault(row[1], []).append(row)
    total = 0
    for rel, rows in by_rel.items():
        assert rel in RELS
        cypher(
            f"UNWIND $rows AS t "
            f"MERGE (h:Entity {{name: t[0]}}) MERGE (tl:Entity {{name: t[2]}}) "
            f"MERGE (h)-[r:{rel}]->(tl) "
            f"ON CREATE SET r.evidence = t[3], r.source = t[4], r.year = t[5], r.freq = 1 "
            f"ON MATCH SET r.freq = coalesce(r.freq,0)+1, r.year = coalesce(r.year, t[5])",
            {"rows": rows},
        )
        total += len(rows)
        print(f"{rel}: {len(rows)}")
    stats = cypher("MATCH (e:Entity) WITH count(e) AS ents MATCH ()-[r]->() "
                   "RETURN ents, count(r) AS rels")
    print("nodes/rels:", stats)


def load_golden_queries():
    """金查询清单: build/golden_queries.tsv, 每行 `描述<TAB>Cypher`; 示例见 examples/。"""
    fp = ROOT / "build" / "golden_queries.tsv"
    if not fp.exists():
        return []
    out = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        if "\t" in line and not line.startswith("#"):
            desc, q = line.split("\t", 1)
            out.append((desc.strip(), q.strip()))
    return out


def run_verify():
    queries = load_golden_queries()
    if not queries:
        print("no golden queries: put build/golden_queries.tsv (see examples/)")
        return
    passed = 0
    for desc, q in queries:
        try:
            rows = cypher(q)
        except Exception as e:
            print(f"[ERR ] {desc}: {e}")
            continue
        sample = "; ".join(str(r) for r in rows[:4])
        status = "PASS" if rows else "EMPTY"
        passed += bool(rows)
        print(f"[{status}] {desc} ({len(rows)} rows) {sample}")
    print(f"golden queries: {passed}/{len(queries)} passed")


if __name__ == "__main__":
    {"extract": run_extract, "load": run_load, "verify": run_verify}[sys.argv[1]]()
