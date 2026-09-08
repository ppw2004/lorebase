"""rag MCP：向量召回 + payload 过滤（契约见 docs/mcp-design.md）。

约束：
- 禁止 import agentscope 或其他 MCP
- LLM/embedding 调用带保险丝与 usage 账本
"""
import json
import os
import time
from pathlib import Path

import requests
from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[3]
QDRANT = os.environ.get("QDRANT_URL", "http://localhost:6333")
COLL = os.environ.get("QDRANT_COLLECTION", "lorebase_chunks")
DASHSCOPE = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"

mcp = FastMCP("rag")
_fails = 0


def _key():
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("DASHSCOPE_API_KEY not set (see .env.example)")
    return key


def embed(query, retries=3):
    global _fails
    for i in range(retries):
        try:
            r = requests.post(DASHSCOPE,
                              headers={"Authorization": f"Bearer {_key()}"},
                              json={"model": "qwen3.7-text-embedding", "input": {"texts": [query]}},
                              timeout=60)
            r.raise_for_status()
            body = r.json()
            with open(ROOT / "build" / "usage_log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%F %T"), "model": "qwen3.7-text-embedding",
                                    "purpose": "rag-search", "in": body.get("usage", {}).get("total_tokens"),
                                    "out": 0}, ensure_ascii=False) + "\n")
            _fails = 0
            return body["output"]["embeddings"][0]["embedding"]
        except Exception as e:
            _fails += 1
            if _fails >= 6:
                raise SystemExit(f"embed fuse: 6 consecutive failures, last: {e}")
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"embed failed after {retries} retries")


@mcp.tool
def search(query: str, kind: str = "", years_min: int | None = None,
           years_max: int | None = None, volume: int | None = None,
           limit: int = 8) -> dict:
    """全向量库语义召回。kind: chunk|entity 留空混合; years/volume 为 chunk 侧精确过滤。
    返回 {results: [{id, kind, score, header?, text|desc?, years?}]}"""
    flt, must = [], []
    if kind in ("chunk", "entity"):
        must.append({"key": "kind", "match": {"value": kind}})
    if volume in (1, 2):
        must.append({"key": "volume", "match": {"value": volume}})
    if years_min is not None or years_max is not None:
        must.append({"key": "years", "range": {"gte": years_min, "lte": years_max}})
    if must:
        flt = [{"must": must}]
    body = {"vector": embed(query), "limit": limit, "with_payload": True}
    if flt:
        body["filter"] = flt[0]
    r = requests.post(f"{QDRANT}/collections/{COLL}/points/search",
                      json=body, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"qdrant search {r.status_code}: {r.text[:200]}")
    out = []
    for h in r.json()["result"]:
        p = h["payload"]
        out.append({
            "id": p.get("chunk_id") or p.get("name"),
            "kind": p.get("kind"),
            "score": round(h["score"], 3),
            "header": f"第{p.get('volume')}卷·{p.get('chapter')}" + (f" > {p.get('section')}" if p.get("section") else ""),
            "text": (p.get("text") or p.get("desc") or "")[:300],
            "years": p.get("years", []),
        })
    return {"results": out}


@mcp.tool
def read_chunk(chunk_id: str) -> dict:
    """读整块正文 + 相邻块(seq±1, 同节/章)供上下文扩展。"""
    r = requests.post(f"{QDRANT}/collections/{COLL}/points/scroll",
                      json={"filter": {"must": [{"key": "chunk_id", "match": {"value": chunk_id}}]},
                            "limit": 1, "with_payload": True, "with_vector": False},
                      timeout=30)
    r.raise_for_status()
    pts = r.json()["result"]["points"]
    if not pts:
        return {"error": "not_found", "hint": f"chunk '{chunk_id}' 不存在"}
    p = pts[0]["payload"]
    neighbors = []
    for d in (-1, 1):
        n = requests.post(f"{QDRANT}/collections/{COLL}/points/scroll",
                          json={"filter": {"must": [
                                    {"key": "chapter", "match": {"value": p["chapter"]}},
                                    {"key": "section", "match": {"value": p.get("section") or ""}},
                                    {"key": "seq", "match": {"value": p["seq"] + d}}]},
                                "limit": 1, "with_payload": True, "with_vector": False},
                          timeout=30)
        n.raise_for_status()
        npts = n.json()["result"]["points"]
        if npts:
            np = npts[0]["payload"]
            neighbors.append({"chunk_id": np["chunk_id"], "seq": np["seq"],
                              "text": np["text"][:400]})
    return {"chunk_id": chunk_id, "header": p["chapter"] + (f" > {p.get('section')}" if p.get("section") else ""),
            "text": p["text"], "years": p.get("years", []), "neighbors": neighbors}


if __name__ == "__main__":
    mcp.run()
