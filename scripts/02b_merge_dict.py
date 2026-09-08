#!/usr/bin/env python3
"""阶段2b: 实体词典终合并。

旧版 union-find 传递闭包会把泛称词(神/大人/舰长)当桥接, 造成巨型实体(实测吸收 189 别名)。
本版策略:
1. 同名候选程序合并(freq 累计)
2. 扇出检测: 一个名字出现在 >FANOUT 个不同实体的 name/alias 里 = 泛称, 全局剔除
3. M3 分批语义归并: 只在"确定同指"时合并, 宁可漏合不可错合
产出 build/entity_dict.json
"""
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module

dm = import_module("02_entity_dict")

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "build" / "entity_raw"
OUT = ROOT / "build" / "entity_dict.json"
FANOUT = 3
BATCH = 80  # 150 会让 M3 思考爆炸顶爆 max_tokens, 输出 0 组(2026-09-03 实测)
BLACKLIST = {"大人", "阁下", "陛下", "国王陛下", "神", "将军", "国王", "老弟", "大哥",
             "小姐姐", "小妹妹", "孩子", "小孩", "少年", "老者", "父亲", "父亲大人",
             "父王", "爸", "妈妈", "盟主", "舰长", "船长", "司令", "长官", "首领",
             "老师", "美男子", "赌徒", "骗子手", "天人", "外星人", "那孩子", "那个少年",
             "公主", "王子", "老女人", "骗子", "盟主陛下"}

MERGE_PROMPT = """任务: 实体归并。同一对象在不同章节可能以不同名字被抽出, 请把下列候选中**确定指向同一对象**的归并成组。

规则(重要):
- 只输出发生了合并的组(≥2个候选); 不确定是否同指的, 宁可漏合不要错合
- 同一轮回/世系下的不同代际个体(如历届继承者)是不同实体, 不可合并; 抽象概念与它的具体 incarnation 也应区分
- 种族/组织/人物/地点是不同类型, 不可跨类型合并
- canonical 选最有辨识度的通名(而非上下文短语如"某某的父亲")

候选列表(主名[别名样本]: 描述 | 类型 | 出现次数):
{cands}

只输出 JSON 数组: [{{"canonical": "主名", "type": "类型", "desc": "一句话描述", "merged": ["被并入的主名", ...]}}]
"""


def norm(s):
    return re.sub(r"\s+", "", str(s or ""))


def load_name_merged():
    entries = {}
    for fp in sorted(RAW_DIR.glob("*.json")):
        try:
            arr = json.loads(fp.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for e in arr:
            name = norm(e.get("name"))
            if not name or len(name) > 40:
                continue
            al = {norm(a) for a in e.get("aliases", []) if 0 < len(norm(a)) <= 40}
            ent = entries.setdefault(name, {"type": e.get("type") or "concept", "desc": "", "freq": 0, "aliases": set()})
            ent["freq"] += 1
            ent["aliases"] |= al
            if len(e.get("desc", "")) > len(ent["desc"]):
                ent["desc"] = e.get("desc", "")
    return entries


def fanout_clean(entries):
    hosts = {}
    for name, ent in entries.items():
        for s in {name} | ent["aliases"]:
            hosts.setdefault(s, set()).add(name)
    generic = {s for s, hs in hosts.items() if len(hs) > FANOUT} | BLACKLIST
    print(f"fanout> {FANOUT} or blacklisted generic names: {len(generic)}")
    out = {}
    dropped_names = 0
    for name, ent in entries.items():
        if name in generic:
            dropped_names += 1
            continue
        al = {a for a in ent["aliases"] if a not in generic and a != name}
        ent["aliases"] = al
        out[name] = ent
    print(f"dropped {dropped_names} generic-named entries, kept {len(out)}")
    return out, generic


def llm_merge(entries):
    by_type = {}
    for name, ent in entries.items():
        by_type.setdefault(ent["type"], []).append(name)
    merges = []
    n_batches = 0
    for typ, names in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        names.sort(key=lambda n: -entries[n]["freq"])
        for i in range(0, len(names), BATCH):
            batch = names[i : i + BATCH]
            lines = []
            for n in batch:
                e = entries[n]
                al = ",".join(sorted(e["aliases"])[:4])
                lines.append(f"{n}[{al}]: {e['desc'][:45]} | {typ} | {e['freq']}")
            prompt = MERGE_PROMPT.format(cands="\n".join(lines))
            try:
                groups = dm.call_m3(prompt, purpose="merge")
            except Exception as ex:
                print(f"merge batch FAIL {typ}#{i}: {ex}")
                continue
            n_batches += 1
            for g in groups if isinstance(groups, list) else []:
                merged = [norm(m) for m in g.get("merged", []) if norm(m)]
                if g.get("canonical") and len(merged) >= 2:
                    merges.append({"canonical": norm(g["canonical"]), "type": g.get("type") or typ,
                                   "desc": g.get("desc", ""), "merged": merged})
            print(f"merge batch {typ}#{i // BATCH}: {len(batch)} cands -> {len(merges)} groups total", flush=True)
    return merges, n_batches


def assemble(entries, merges):
    idx = {n: dict(e) for n, e in entries.items()}
    for g in merges:
        root = g["canonical"]
        if root not in idx:
            root = next((m for m in g["merged"] if m in idx), None)
            if not root:
                continue
        r = idx[root]
        r["freq"] += sum(idx[m]["freq"] for m in g["merged"] if m in idx and m != root)
        r["aliases"] = set(r["aliases"])
        for m in g["merged"]:
            if m in idx and m != root:
                r["aliases"] |= idx[m]["aliases"] | {m}
                if len(idx[m]["desc"]) > len(r["desc"]):
                    r["desc"] = idx[m]["desc"]
                del idx[m]
        if g.get("desc") and not r["desc"]:
            r["desc"] = g["desc"]
    result = [
        {"canonical": n, "type": e["type"], "aliases": sorted(e["aliases"]),
         "desc": e["desc"], "freq": e["freq"]}
        for n, e in idx.items()
    ]
    result.sort(key=lambda x: -x["freq"])
    return result


def main():
    dm.check_quota()
    entries = load_name_merged()
    print(f"name-merged candidates: {len(entries)}")
    entries, _ = fanout_clean(entries)
    t0 = time.time()
    merges, nb = llm_merge(entries)
    print(f"llm merge: {nb} batches, {len(merges)} merge groups, {time.time()-t0:.0f}s")
    with open(ROOT / "build" / "merge_groups.json", "w", encoding="utf-8") as f:
        json.dump(merges, f, ensure_ascii=False, indent=1)
    result = assemble(entries, merges)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"final dict: {len(result)} entities")
    print("type dist:", Counter(x["type"] for x in result).most_common())
    top = result[0]
    print(f"top entity: {top['canonical']} freq={top['freq']} n_aliases={len(top['aliases'])}")


if __name__ == "__main__":
    main()
