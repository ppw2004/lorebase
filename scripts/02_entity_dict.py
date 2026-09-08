#!/usr/bin/env python3
"""阶段2: 逐单元(节/章)用 MiniMax-M3 抽实体候选, 合并去重出实体词典。

产物:
- build/entity_raw/*.json   每单元抽取结果(断点续跑: 已存在则跳过)
- build/entity_dict.json    合并词典 [{canonical, type, aliases, desc, freq}]
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

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "build" / "entity_raw"
OUT = ROOT / "build" / "entity_dict.json"
API = "https://api.minimaxi.com/v1/chat/completions"
THINK = re.compile(r"<think>.*?</think>", re.S)
CONCURRENCY = 4

PROMPT = """你是小说世界观知识库的构建助手。请从下面的文本中抽取所有命名实体（人物、种族、组织/势力、地点、物品、概念/设定、事件），输出 JSON 数组，每个元素格式：
{"name": "规范主名", "type": "person|race|faction|location|item|concept|event", "aliases": ["别名/称号/绰号/马甲"], "desc": "一句话描述(50字内)"}
要求：
- 别名务必收全，同一对象的不同称呼、代号、尊称都归并到同一实体
- 只依据文本，不要编造
- 只输出 JSON 数组本身，不要任何其他文字或代码围栏

文本：
"""


def load_key():
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        raise RuntimeError("MINIMAX_API_KEY not set (see .env.example)")
    return key


KEY = load_key()

# 用量保险丝: 连续失败熔断 + 启动前额度检查
FUSE_LIMIT = 6
_fuse = {"fails": 0}


class FuseBlown(RuntimeError):
    pass


def check_quota(min_weekly_pct=10):
    r = requests.get("https://www.minimaxi.com/v1/token_plan/remains",
                     headers={"Authorization": f"Bearer {KEY}"}, timeout=15)
    r.raise_for_status()
    for g in r.json().get("model_remains", []):
        if g.get("model_name") == "general":
            pct = g.get("current_weekly_remaining_percent")
            print(f"minimax weekly quota remaining: {pct}%")
            if pct is not None and pct < min_weekly_pct:
                raise SystemExit(f"quota fuse: weekly remaining {pct}% < {min_weekly_pct}%, abort")
            return
    raise SystemExit("quota fuse: cannot read minimax quota, abort")


def _close_json(b):
    """按括号栈补齐截断的 JSON (忽略字符串字面量内的括号)."""
    stack, in_str, esc = [], False, False
    for ch in b:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "[{":
            stack.append(ch)
        elif ch in "]}" and stack:
            stack.pop()
    return b + "".join("]" if c == "[" else "}" for c in reversed(stack))


def extract_json(content):
    s = THINK.sub("", content).strip()
    s = re.sub(r"^```(json)?|```$", "", s, flags=re.M).strip()
    i_obj, i_arr = s.find("{"), s.find("[")
    if i_arr >= 0 and (i_obj < 0 or i_arr < i_obj):
        opener, closer, i = "[", "]", i_arr  # 最先出现的括号 = 外层类型
    elif i_obj >= 0:
        opener, closer, i = "{", "}", i_obj
    else:
        raise ValueError("no JSON object/array found")
    j = s.rfind(closer)
    if j <= i:
        j = len(s) - 1  # 无闭合: 视为截断走修复
    body = s[i : j + 1]
    try:
        return json.loads(body)
    except ValueError:
        pass
    for fixed in (re.sub(r",\s*([\]}])", r"\1", body), body):  # 先去尾逗号再栈补齐
        try:
            return json.loads(_close_json(fixed))
        except ValueError:
            continue
    raise ValueError("unparseable JSON")


def log_usage(purpose, usage, model="MiniMax-M3"):
    if not usage:
        return
    rec = {"ts": time.strftime("%F %T"), "model": model, "purpose": purpose,
           "in": usage.get("prompt_tokens"), "out": usage.get("completion_tokens"),
           "reasoning": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")}
    with open(ROOT / "build" / "usage_log.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def call_m3(prompt, retries=4, purpose="dict"):
    """prompt 为完整 user 消息文本(调用方自行拼装)."""
    last = None
    for _ in range(retries + 1):
        try:
            r = requests.post(
                API,
                headers={"Authorization": f"Bearer {KEY}"},
                json={
                    "model": "MiniMax-M3",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 32768,  # 16384 会被思考顶爆导致 JSON 截断(2026-09-03 实测)
                },
                timeout=600,  # 长思考单元可超 5 分钟, 300s 会 ReadTimeout(2026-09-03 实测)
            )
            r.raise_for_status()
            body = r.json()
            log_usage(purpose, body.get("usage"))
            out = extract_json(body["choices"][0]["message"]["content"] or "")
            _fuse["fails"] = 0
            return out
        except Exception as e:
            last = e
            _fuse["fails"] += 1
            if _fuse["fails"] >= FUSE_LIMIT:
                raise FuseBlown(f"{FUSE_LIMIT} consecutive LLM failures, last: {e}") from e
            time.sleep(5)
    raise last


def unit_key(chapter, section):
    k = f"{chapter}-{section}" if section else chapter
    return re.sub(r"[^\w.\-]", "", k)


def iter_units():
    """yield (vol, uid, header_text, chapter, section); 同名章加序号消歧。"""
    seen = {}
    for vol, path in enumerate(chunking.corpus_files(), 1):
        for chapter, section, paras in chunking.parse_units(path):
            base = unit_key(chapter, section)
            n = seen.get((vol, base), 0) + 1
            seen[(vol, base)] = n
            uid = f"v{vol}_{base}" + (f"~{n}" if n > 1 else "")
            header = f"【v{vol}·{chapter + ' > ' + section if section else chapter}】"
            yield vol, uid, header + "\n" + "\n".join(paras), chapter, section


def main():
    check_quota()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    units = [(vol, uid, text) for vol, uid, text, _, _ in iter_units()]
    print(f"units={len(units)}")

    def work(vol, key, text):
        out = RAW_DIR / f"{key}.json"
        if out.exists():
            return key, "cached", 0
        ents = call_m3(PROMPT + text)
        out.write_text(json.dumps(ents, ensure_ascii=False), encoding="utf-8")
        return key, "ok", len(ents)

    t0 = time.time()
    done = fail = 0
    total_ents = 0
    with ThreadPoolExecutor(CONCURRENCY) as ex:
        futs = [ex.submit(work, v, k, t) for v, k, t in units]
        for f in as_completed(futs):
            try:
                key, status, n = f.result()
                done += 1
                total_ents += n
                print(f"[{done}/{len(units)}] {key} {status} ents={n} elapsed={time.time()-t0:.0f}s", flush=True)
            except FuseBlown as e:
                fail += 1
                print(f"FUSE BLOWN: {e} — aborting, progress kept for resume", flush=True)
                for f2 in futs:
                    f2.cancel()
                break
            except Exception as e:
                fail += 1
                print(f"FAIL({fail}): {type(e).__name__}: {e}", flush=True)
    print(f"extract done: ok={done - fail} fail={fail} total_entities={total_ents}")


if __name__ == "__main__":
    main()
