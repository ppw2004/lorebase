# examples/

自造示例语料与配套产物，用于演示与验证全管线。**不含任何真实作品内容**。

- `corpus/star-isles.md`：自造短篇设定集《星屿纪年》（约 1600 字，3 章 9 节），埋入了 7 类实体、多种关系与纪年（`纪元前300年` / `405年`）
- `golden_queries.tsv`：9 条金查询（图谱建成后应全部非空），复制到 `build/golden_queries.tsv` 后用 `python3 scripts/03_extract_graph.py verify` 验收

## 跑通示例

```bash
mkdir -p corpus && cp examples/corpus/star-isles.md corpus/
export MINIMAX_API_KEY=... NEO4J_PASSWORD=... DASHSCOPE_API_KEY=...
export LOREBASE_SKIP_TITLES=星屿纪年   # 跳过书名标题(否则会被当成一章)
python3 scripts/01_chunking.py          # 切片
python3 scripts/02_entity_dict.py       # 实体抽取(M3)
python3 scripts/02b_merge_dict.py       # 词典归并
python3 scripts/03_extract_graph.py extract && python3 scripts/03_extract_graph.py load
cp examples/golden_queries.tsv build/golden_queries.tsv
python3 scripts/03_extract_graph.py verify
python3 scripts/04_embed_qdrant.py && python3 scripts/04_embed_qdrant.py verify "灯语是什么"
```

全部通过后，示例图谱即可用于问答服务（`agent/web/`）与校验智能体（`agent/runner.py --sample 3`）。
