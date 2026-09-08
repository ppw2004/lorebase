# Lorebase

English | [中文](README.md)

Narrative text → dual (vector + graph) knowledge base → MCP agent validation → AG-UI streaming QA.

A complete pipeline that turns "reading a book series" into "a searchable, reason-over-able, conversational worldview knowledge base":

```
your corpus (markdown)              your LLM / vector / graph stores
     │                                      │
 01 chunking ──► 02/02b entity dict ─► 03 graph ─► Neo4j ──┐
     │                │                                    ├─► graph-read ─┐
     └──── 04 embeddings ─────────► Qdrant ──► rag ───────┤               ├─► validation agent (writes weights / revisions / intervals)
                                                           └─ graph-write ─┘
                                                                            AG-UI QA service (read-only)
```

## Features

- **Structured chunking**: chapter/section boundaries + ~500-char greedy aggregation + tail overlap + chunk-header context into the embeddings (worldbuilding text is uninterpretable out of chapter context)
- **Dictionary first**: LLM extraction + fan-out cleaning (generic-term removal) + LLM semantic merging — alias-merge quality is the first-order variable of graph quality
- **Division of labor between stores**: Qdrant finds relevant source text, Neo4j does relation traversal and as-of temporal queries; `MENTIONED_IN` edges + chunk_id interlink both directions
- **MCP tool layer**: graph-read (read-only thin layer, write-statement interception) / graph-write (parameterized Cypher + 14-relation whitelist, no raw LLM-assembled statements) / rag (vector recall + payload filtering)
- **Agent validation loop**: chunk-by-chunk cross-check of "source text ↔ graph subgraph ↔ vector recall"; visits = hard weight, verdicts = soft weight, revisions always carry source evidence and a Langfuse trace link — every graph change is replayable
- **AG-UI streaming QA**: reasoning, tool calls, and answer text stream in true temporal order (AgentScope native event stream → SSE)

## Applied case

[ppw2004/kuiba-worldview](https://github.com/ppw2004/kuiba-worldview) — the first full knowledge base built with this framework, from the setting text of a two-volume Chinese novel: 566 chunks → 1,624 entities / 3,555 relations; 562 validation units at 99.8% completion and a 10/10 golden-query regression. The case publishes its dataset (including 20,202 validation trace events), a 13:50 four-act graph-rendering short film, and a live QA site.

## Quick start

Prerequisites: Python 3.11+, Neo4j 5.x, Qdrant 1.x, MiniMax and DashScope API keys (all variables in `.env.example`).

```bash
# 1) Pipeline: examples/ ships a 1,600-char self-made corpus — run the whole chain for free
cp .env.example .env && vi .env  # fill in keys, then: source .env
mkdir -p corpus && cp examples/corpus/star-isles.md corpus/
export LOREBASE_SKIP_TITLES=星屿纪年  # skip the book-title heading (adapt to your corpus)
python3 scripts/01_chunking.py
python3 scripts/02_entity_dict.py && python3 scripts/02b_merge_dict.py
python3 scripts/03_extract_graph.py extract && python3 scripts/03_extract_graph.py load
cp examples/golden_queries.tsv build/golden_queries.tsv
python3 scripts/03_extract_graph.py verify   # golden queries should all PASS
python3 scripts/04_embed_qdrant.py

# 2) Your own corpus: convert epub to markdown first; multi-volume files in corpus/ sort by filename
python3 scripts/convert_epub_to_md.py book.epub corpus/vol1.md

# 3) Validation agent (optional; writes back weights/revisions)
python3 -m venv agent/.venv && agent/.venv/bin/pip install -r agent/web/requirements-main.txt
python3 -m venv agent/.venv-mcp && agent/.venv-mcp/bin/pip install -r agent/web/requirements-mcp.txt
vi docs/prompts/node-validation.prompt.md   # rewrite for your corpus, set frontmatter to approved (unapproved prompts refuse to load)
agent/.venv/bin/python agent/runner.py --sample 3

# 4) QA service (read-only dual MCP)
vi docs/prompts/qa.prompt.md                # likewise set approved
agent/.venv/bin/python -m uvicorn agent.web.server:app --port 8300
# or: docker build -t lorebase-qa . && docker run -p 8300:8300 --env-file .env lorebase-qa
```

Detailed designs live in [docs/](docs/) (architecture / graph schema / MCP contracts / agent design / ADRs).

## Using the MCPs directly (without agents)

The three MCPs are standard stdio servers you can plug into any MCP client (Cherry Studio / Claude Desktop / Cursor, etc.) to operate the dual knowledge base you built:

| MCP | Tools | Purpose |
|---|---|---|
| **graph-read** | `get_schema` / `read_cypher` / `neighbors` / `chunk_entities` | Read-only graph access: ontology, write-intercepted free queries, multi-hop neighborhoods, chunk-level reverse lookup |
| **rag** | `search` / `read_chunk` | Vector recall + payload filters (kind / year range / volume), adjacent-chunk expansion |
| **graph-write** | `record_visit` / `vote` / `revise_description` / `set_interval` / `add_relation` / `fix_relation` | Whitelisted write-back: parameterized Cypher + 14-relation-type whitelist, no raw LLM-assembled statements (validation-agent use only) |

Contract details in [docs/mcp-design.md](docs/mcp-design.md).

```json
{
  "mcpServers": {
    "lorebase-graph-read": {
      "command": "agent/.venv-mcp/bin/python",
      "args": ["agent/mcps/graph_read/server.py"],
      "cwd": "/path/to/lorebase",
      "env": { "NEO4J_HTTP": "http://localhost:7474", "NEO4J_PASSWORD": "..." }
    },
    "lorebase-rag": {
      "command": "agent/.venv-mcp/bin/python",
      "args": ["agent/mcps/rag/server.py"],
      "cwd": "/path/to/lorebase",
      "env": { "QDRANT_URL": "http://localhost:6333", "QDRANT_COLLECTION": "lorebase_chunks", "DASHSCOPE_API_KEY": "..." }
    }
  }
}
```

Configure graph-write (validation-agent-only whitelisted writes) the same way if needed. All environment variables are listed in `.env.example`.

## Corpus & compliance

- This repository **ships no real-work corpus**; examples/ is self-made with entirely fictional proper nouns
- When building on your own corpus, make sure you hold the appropriate rights; the resulting knowledge graph and retrieval service are for personal research only
- This project is not affiliated with any novelist or publisher and does not endorse third-party content built on this framework

## License

Apache-2.0
