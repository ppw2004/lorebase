#!/usr/bin/env python3
"""阶段4: chunk + 实体向量灌 Qdrant (默认 lorebase_chunks collection)。

- chunk: embed(text_ctx 带【卷·章>节】块头), payload 含 years 时间线
- entity: 从 Neo4j 拉实体, embed("主名(别名): 描述"), payload kind=entity
- point id = uuid5(chunk_id/entity:name), upsert 幂等可重跑
- dashscope 兼容模式单批 ≤20

用法:
  python3 04_embed_qdrant.py                    # chunks + entities
  python3 04_embed_qdrant.py chunks             # 只灌 chunk 向量
  python3 04_embed_qdrant.py verify "查询词"    # 检索抽查
"""
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
QDRANT = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLL = os.environ.get("QDRANT_COLLECTION", "lorebase_chunks")
DASHSCOPE = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # url namespace

DASH_KEY = os.environ.get("DASHSCOPE_API_KEY")
if not DASH_KEY:
    raise RuntimeError("DASHSCOPE_API_KEY not set (see .env.example)")


_embed_fails = 0


def embed(texts, retries=3):
    global _embed_fails
    for i in range(retries):
        try:
            r = requests.post(DASHSCOPE,
                              headers={"Authorization": f"Bearer {DASH_KEY}"},
                              json={"model": "qwen3.7-text-embedding", "input": {"texts": texts}},
                              timeout=60)
            r.raise_for_status()
            body = r.json()
            usage = body.get("usage", {})
            with open(ROOT / "build" / "usage_log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%F %T"), "model": "qwen3.7-text-embedding",
                                    "purpose": "embed", "in": usage.get("total_tokens"), "out": 0},
                                   ensure_ascii=False) + "\n")
            _embed_fails = 0
            return [e["embedding"] for e in body["output"]["embeddings"]]
        except Exception as e:
            _embed_fails += 1
            if _embed_fails >= 6:
                raise SystemExit(f"embed fuse: 6 consecutive dashscope failures, last: {e}")
            time.sleep(3 * (i + 1))


def ensure_collection():
    r = requests.get(f"{QDRANT}/collections/{COLL}", timeout=10)
    if r.status_code == 200 and r.json().get("result"):
        print(f"collection {COLL} exists")
        return
    r = requests.put(f"{QDRANT}/collections/{COLL}", json={
        "vectors": {"size": 1024, "distance": "Cosine"},
        "shard_number": 1, "replication_factor": 2,
    }, timeout=30)
    r.raise_for_status()
    print(f"created {COLL}")


def upsert_batches(items):
    """items: [(deterministic_key, text, payload)]"""
    total = 0
    for i in range(0, len(items), 20):
        batch = items[i : i + 20]
        vecs = embed([t for _, t, _ in batch])
        points = [
            {"id": str(uuid.uuid5(NS, key)), "vector": v, "payload": p}
            for (key, _, p), v in zip(batch, vecs)
        ]
        r = requests.put(f"{QDRANT}/collections/{COLL}/points?wait=true",
                         json={"points": points}, timeout=60)
        r.raise_for_status()
        total += len(points)
        print(f"upserted {total}/{len(items)}", flush=True)


def embed_chunks():
    recs = [json.loads(l) for l in open(ROOT / "build" / "chunks.jsonl", encoding="utf-8")]
    items = [
        (r["chunk_id"], r["text_ctx"],
         {"kind": "chunk", "chunk_id": r["chunk_id"], "volume": r["volume"],
          "chapter": r["chapter"], "section": r["section"], "seq": r["seq"],
          "text": r["text"], "years": r["years"]})
        for r in recs
    ]
    upsert_batches(items)


def embed_entities():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from importlib import import_module
    g = import_module("03_extract_graph")
    rows = g.cypher("MATCH (e:Entity) RETURN e.name AS n, e.aliases AS a, e.type AS t, e.description AS d")
    items = []
    for (n, a, t, d) in rows:
        alias_s = ",".join(a or [])
        text = f"{n}({alias_s})" + (f": {d[:200]}" if d else "")
        items.append((f"entity:{n}", text,
                      {"kind": "entity", "name": n, "aliases": a or [], "type": t, "desc": d or ""}))
    print(f"entities from neo4j: {len(items)}")
    upsert_batches(items)


def verify(query):
    vec = embed([query])[0]
    r = requests.post(f"{QDRANT}/collections/{COLL}/points/search", json={
        "vector": vec, "limit": 5, "with_payload": True,
    }, timeout=30)
    r.raise_for_status()
    print(f"\nquery: {query}")
    for h in r.json()["result"]:
        p = h["payload"]
        head = p.get("chunk_id") or p.get("name")
        print(f"  {h['score']:.3f} [{p.get('kind')}] {head} | {(p.get('text') or p.get('desc') or '')[:60]}")


if __name__ == "__main__":
    if "verify" in sys.argv:
        for q in sys.argv[sys.argv.index("verify") + 1:]:
            verify(q)
        sys.exit(0)
    ensure_collection()
    if "chunks" in sys.argv or len(sys.argv) == 1:
        embed_chunks()
    if "entities" in sys.argv or len(sys.argv) == 1:
        embed_entities()
