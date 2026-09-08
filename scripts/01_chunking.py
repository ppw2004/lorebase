#!/usr/bin/env python3
"""阶段1: 语料结构化切片，产出 build/chunks.jsonl。

切法（结构边界优先）:
- 单元 = markdown 的章(# )/节(## ); 单元内段落贪心聚合
- 目标 ~500 字, min 150 / max 800; 超长段落按句子边界二次切
- 相邻块带上一块尾段作重叠, 保指代语境
- 每块产出 text(纯正文) 与 text_ctx(块头路径上下文, 供 embedding)
- years: 按纪年正则抽年份进 payload, 支持时间线过滤检索

语料配置:
- 默认扫 corpus/*.md(按文件名排序, 序号即卷号); 或环境变量 LOREBASE_CORPUS(冒号分隔的多文件)
- 书名/元信息标题跳过: LOREBASE_SKIP_TITLES(逗号分隔)
- 纪年正则: LOREBASE_YEAR_RE, 须含两个捕获组(纪元前年份/纪元内年份), 默认「前N年/N年」
"""
import json
import os
import re
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "chunks.jsonl"
TARGET, MIN, MAXC = 500, 150, 800
YEAR_RE = re.compile(os.environ.get("LOREBASE_YEAR_RE")
                     or r"(?:公元?前|纪元前)(\d{1,4})年|(\d{3,4})年")
SKIP_TITLES = {t.strip() for t in os.environ.get("LOREBASE_SKIP_TITLES", "").split(",") if t.strip()}
SENT_SPLIT = re.compile(r"(?<=[。！？…!”」』])")


def corpus_files():
    """语料文件列表, 按返回顺序编号卷号(1 起)。"""
    if os.environ.get("LOREBASE_CORPUS"):
        return [Path(p) for p in os.environ["LOREBASE_CORPUS"].split(":") if p]
    d = ROOT / "corpus"
    files = sorted(d.glob("*.md")) if d.exists() else []
    if not files:
        raise SystemExit("no corpus: put markdown files in corpus/ or set LOREBASE_CORPUS")
    return files


def parse_units(path):
    """yield (chapter, section, paragraphs)；书名标题(在跳过列表)不产出单元。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    units, chapter, section, buf = [], "", "", []

    def flush():
        nonlocal buf
        if buf and (chapter or section):
            units.append((chapter, section, buf))
        buf = []

    for line in lines:
        line = line.strip()
        if line.startswith("# ") and not line.startswith("## "):
            flush()
            chapter, section = "", ""
            title = line[2:].strip()
            if title in SKIP_TITLES:
                continue
            chapter = title
        elif line.startswith("## "):
            flush()
            section = line[3:].strip()
        elif line.startswith("!["):
            continue  # 插图引用不入 chunk 正文
        elif line:
            buf.append(line)
    flush()
    return [u for u in units if u[0] or u[1]]


def split_long_para(para):
    if len(para) <= MAXC:
        return [para]
    sents = SENT_SPLIT.split(para)
    out, cur = [], ""
    for s in sents:
        if len(cur) + len(s) > MAXC and cur:
            out.append(cur)
            cur = s
        else:
            cur += s
    if cur:
        out.append(cur)
    return out


def chunk_unit(paras):
    """段落贪心聚合 + 尾段重叠。"""
    paras = [p for para in paras for p in split_long_para(para)]
    blocks, cur, size = [], [], 0
    for p in paras:
        cur.append(p)
        size += len(p)
        if size >= TARGET:
            blocks.append(cur)
            cur, size = [], 0
    if cur:
        if size < MIN and blocks and len("".join(blocks[-1])) + size <= MAXC:
            blocks[-1].extend(cur)
        else:
            blocks.append(cur)
    # 重叠: 除首块外, 前块尾段(<=300字)前置
    out = []
    for i, b in enumerate(blocks):
        if i > 0 and blocks and len(prev_tail := blocks[i - 1][-1]) <= 300:
            out.append([prev_tail] + b)
        else:
            out.append(b)
    return out


def extract_years(text):
    ys = set()
    for pre, post in YEAR_RE.findall(text):
        ys.add(-int(pre) if pre else int(post))
    return sorted(ys)


def iter_chunks():
    """yield (vol, chapter, section, seq, text)。"""
    for vol, path in enumerate(corpus_files(), 1):
        for chapter, section, paras in parse_units(path):
            blocks = chunk_unit(paras)
            for seq, b in enumerate(blocks, 1):
                yield vol, chapter, section, seq, "\n".join(b)


def main():
    records = []
    for vol, chapter, section, seq, text in iter_chunks():
        unit = f"{chapter} > {section}" if section else chapter
        header = f"【v{vol}·{unit}】"
        cid = f"v{vol}-ch{chapter}-{'s' + section.split(' ')[0] if section else 'w'}-{seq:02d}"
        cid = re.sub(r"[^\w.\-]", "", cid)
        records.append(
            {
                "chunk_id": cid,
                "volume": vol,
                "chapter": chapter,
                "section": section,
                "seq": seq,
                "text": text,
                "text_ctx": header + "\n" + text,
                "years": extract_years(text),
                "char_len": len(text),
            }
        )
    OUT.parent.mkdir(exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    lens = [r["char_len"] for r in records]
    with_years = sum(1 for r in records if r["years"])
    print(f"chunks={len(records)}")
    print(f"size: min={min(lens)} median={statistics.median(lens):.0f} max={max(lens)} total={sum(lens)}")
    print(f"chunks with years: {with_years}/{len(records)}")


if __name__ == "__main__":
    main()
