"""校验 runner: 遍历 chunk, 断点续跑, Langfuse trace, 心得沉淀, 熔断。

用法:
  agent/.venv/bin/python agent/runner.py --sample 10   # 抽样试跑: 均匀抽 10 个 chunk
  agent/.venv/bin/python agent/runner.py               # 全量(未跑过的)
Langfuse 三件套环境变量存在则上报 trace(一 chunk 一 trace)。
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent
ROOT = AGENT_DIR.parent
sys.path.insert(0, str(AGENT_DIR))
from agent import run_chunk  # noqa: E402

OUT_DIR = ROOT / "build" / "validation"
USAGE_LOG = ROOT / "build" / "usage_log.jsonl"
AGENT_MD = AGENT_DIR / "domain-knowledge.md"
FUSE = 3


def chunks():
    return [json.loads(l) for l in open(ROOT / "build" / "chunks.jsonl", encoding="utf-8")]


def sample_ids(n):
    ids = [r["chunk_id"] for r in chunks()]
    if n >= len(ids):
        return ids
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]


def langfuse():
    import os
    envf = AGENT_DIR / ".langfuse.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v.strip('"'))
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse
        return Langfuse(host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"))
    except Exception:
        return None


def parse_reply(text):
    """从回复文本提取结论 JSON(容错: 剥围栏/截取大括号)。"""
    import re
    s = re.sub(r"^```(json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i : j + 1])
    except ValueError:
        return None


def log_usage(usage):
    if not usage:
        return
    rec = {"ts": time.strftime("%F %T"), "model": "MiniMax-M3", "purpose": "validate",
           "in": usage.get("input_tokens") or usage.get("prompt_tokens"),
           "out": usage.get("output_tokens") or usage.get("completion_tokens")}
    with open(USAGE_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_insight(chunk_id, insight):
    if not insight or not insight.strip():
        return
    line = f"- [{time.strftime('%F %H:%M')}] {chunk_id}: {insight.strip()[:200]}\n"
    body = AGENT_MD.read_text(encoding="utf-8") if AGENT_MD.exists() else ""
    if "## 运行日志" not in body:
        body += "\n## 运行日志（智能体心得自动追加区）\n"
    AGENT_MD.write_text(body.rstrip("\n") + "\n" + line, encoding="utf-8")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, metavar="N",
                    help="均匀抽样 N 个 chunk 试跑")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    all_chunks = chunks()
    ids = sample_ids(args.sample) if args.sample else [r["chunk_id"] for r in all_chunks]
    ids = [i for i in ids if any(r["chunk_id"] == i for r in all_chunks)]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    todo = [i for i in ids if not (OUT_DIR / f"{i}.json").exists()]
    print(f"target={len(ids)} todo={len(todo)}")

    lf = langfuse()
    if lf is None:
        print("langfuse: 未配置三件套, 跳过上报")
    from agent import build_toolkit
    toolkit, mcps = await build_toolkit()  # 全程复用, 避免逐 chunk 重启 MCP
    fails = 0
    total_fails = 0
    for n, chunk_id in enumerate(todo, 1):
        t0 = time.time()
        try:
            content, usage = await run_chunk(chunk_id, toolkit=toolkit, mcps=mcps)
            verdict = parse_reply(content)
            if verdict is None:
                # M3 偶发 think-only 终止(把回合结束在思考里): 即时重试一次, 历史重试通过率高
                print(f"[{n}/{len(todo)}] {chunk_id} think-only, 即时重试", flush=True)
                content, usage = await run_chunk(chunk_id, toolkit=toolkit, mcps=mcps)
                verdict = parse_reply(content)
                if verdict is None:
                    raise ValueError(f"reply 无 JSON(重试后仍无): {content[:120]}")
            (OUT_DIR / f"{chunk_id}.json").write_text(
                json.dumps({"chunk_id": chunk_id, "ts": time.strftime("%F %T"),
                            "verdicts": verdict.get("verdicts", []),
                            "insights": verdict.get("insights", ""),
                            "raw": content}, ensure_ascii=False, indent=1), encoding="utf-8")
            append_insight(chunk_id, verdict.get("insights", ""))
            log_usage(usage)
            if lf:
                with lf.start_as_current_observation(name=f"validate:{chunk_id}",
                                                     as_type="generation") as span:
                    span.update(input=chunk_id, output=content[:2000],
                                metadata={**usage, "session": f"validate-sample{args.sample}" if args.sample else "validate-full"})
                lf.flush()
            fails = 0
            print(f"[{n}/{len(todo)}] {chunk_id} ok verdicts={len(verdict.get('verdicts', []))} "
                  f"{time.time()-t0:.0f}s", flush=True)
        except Exception as e:
            fails += 1
            total_fails += 1
            print(f"[{n}/{len(todo)}] {chunk_id} FAIL({fails}) {type(e).__name__}: {str(e)[:150]}", flush=True)
            if fails >= FUSE:
                # 软熔断: 冷却后跳过继续, 失败 chunk 留待下轮补跑; 硬熔断防系统性故障烧额度
                print(f"SOFT-FUSE: 连续{FUSE}败, 冷却120s后继续 (累计失败 {total_fails})", flush=True)
                await asyncio.sleep(120)
                fails = 0
            if total_fails >= 20:
                print("HARD-FUSE: 累计20败, 疑系统性故障, 中止", flush=True)
                break
    print("runner done")


if __name__ == "__main__":
    asyncio.run(main())
