"""问答 web 服务: AgentScope 原生 AG-UI 流式问答(模型/思考/工具调用全程流式可见)。

最原始路径: agent.reply_stream() 事件流 → agentscope 官方 AGUIProtocolMiddleware
转 AG-UI 事件 → SSE (data: {json}\n\n, 与 agentscope.app 官方线格式一致)。
工具: graph-read + rag (纯只读, 不挂 graph-write)。
提示词: docs/prompts/qa.prompt.md (须 approved, 未审批拒载)。
运行: agent/.venv/bin/python -m uvicorn agent.web.server:app --host 0.0.0.0 --port 8300
"""
import asyncio
import os
import json
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from agent.agent import build_toolkit, minimax_key  # noqa: E402

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
PROMPT_FILE = ROOT / os.environ.get("LOREBASE_QA_PROMPT", "docs/prompts/qa.prompt.md")
STATIC_DIR = Path(__file__).resolve().parent / "static"
SESSION_CAP = 50
MAX_ITERS = 12

_state: dict = {}


def load_qa_prompt() -> str:
    raw = PROMPT_FILE.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if not m or "approved" not in m.group(1):
        raise RuntimeError(f"prompt not approved: {PROMPT_FILE}")
    return m.group(2)


def load_domain_knowledge() -> str:
    """agent/domain-knowledge.md 领域知识节(不带校验策略/运行日志, 防角色污染); 可选。"""
    fp = ROOT / "agent" / "domain-knowledge.md"
    if not fp.exists():
        return ""
    s = fp.read_text(encoding="utf-8")
    m = re.search(r"^## 领域知识.*?\n(.*?)(?=^## )", s, re.S | re.M)
    return m.group(1).strip() if m else ""


def build_qa_agent(toolkit):
    from agentscope.agent import Agent, ReActConfig
    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel
    from agentscope.permission import PermissionContext, PermissionMode
    from agentscope.state import AgentState

    model = OpenAIChatModel(
        credential=OpenAICredential(api_key=minimax_key(),
                                    base_url="https://api.minimaxi.com/v1"),
        model="MiniMax-M3",
        parameters=OpenAIChatModel.Parameters(max_tokens=16384),
        stream=True,  # 流式: reply_stream 逐 token 吐 Text/Thinking delta
        context_size=1000000,
        max_retries=3,
    )
    system = load_qa_prompt()
    dk = load_domain_knowledge()
    if dk:
        system += "\n\n# 领域知识（硬事实，回答时必须遵守）\n" + dk
    middlewares = []
    if _state.get("langfuse"):
        from agent.agent import SafeTracingMiddleware

        middlewares.append(SafeTracingMiddleware())
    state = AgentState(permission_context=PermissionContext(mode=PermissionMode.BYPASS))
    return Agent(name="lorebase-qa", system_prompt=system, model=model,
                 toolkit=toolkit, state=state, middlewares=middlewares,
                 react_config=ReActConfig(max_iters=MAX_ITERS))


def init_langfuse() -> bool:
    """加载 agent/web/.langfuse.env 并注册全局 client。
    必须先于任何 Agent 实例化(OTel provider 注册顺序, ADR-002)。"""
    envf = Path(__file__).resolve().parent / ".langfuse.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v.strip('"'))
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return False
    from langfuse import Langfuse

    Langfuse(host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"))
    return True


@asynccontextmanager
async def lifespan(app):
    load_qa_prompt()  # 未审批直接拒启
    lf_on = init_langfuse()
    toolkit, mcps = await build_toolkit(("graph-read", "rag"))
    _state.update(toolkit=toolkit, mcps=mcps, sessions={}, lock=asyncio.Lock(),
                  langfuse=lf_on)
    print(f"langfuse: {'on' if lf_on else 'off(未配置)'}", flush=True)
    try:
        yield
    finally:
        for c in mcps:
            try:
                await c.close()
            except BaseException:
                pass


app = FastAPI(title="lorebase-qa", lifespan=lifespan)


def _agui_payload(event):
    """AgentScope 事件 → AG-UI 官方事件 dict (复用 agentscope 自带转换器)。"""
    from agentscope.app.middleware._protocol._agui import AGUIProtocolMiddleware

    ev = AGUIProtocolMiddleware(None)._to_agui_event(event)  # noqa: SLF001
    return ev.model_dump(mode="json", exclude_none=True, by_alias=True)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/chat")
async def chat(request: Request):
    from agentscope.message import Msg

    body = await request.json()
    session_id = body.get("session_id") or str(uuid.uuid4())
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "question required"}, status_code=400)

    sessions: dict = _state["sessions"]
    lock: asyncio.Lock = _state["lock"]
    history: list = sessions.setdefault(session_id, [])
    while len(sessions) > SESSION_CAP:
        sessions.pop(next(iter(sessions)))
    user_msg = Msg(name="user", role="user",
                   content=[{"type": "text", "text": question}])

    async def gen():
        # MCP stdio 单流: 问答串行; 等 lock 期间无事件, 前端转圈即可
        async with lock:
            from agentscope.message import Msg as _Msg

            from agent.agent import _content_text

            history.append(user_msg)
            agent = build_qa_agent(_state["toolkit"])

            def _root():
                """langfuse 根 span(会话维度); 未配置时返回 nullcontext 等价物。"""
                if not _state.get("langfuse"):
                    import contextlib

                    return contextlib.nullcontext(None)
                from langfuse import get_client

                return get_client().start_as_current_observation(
                    name="qa-chat", as_type="chain",
                    trace_context={"session_id": session_id})

            with _root() as obs:
                if obs:
                    obs.update(input=question,
                               metadata={"service": "lorebase-qa", "session": session_id})
                try:
                    async for ev in agent.reply_stream(history, yield_final_msg=True):
                        if isinstance(ev, _Msg):  # 最终回复落会话历史
                            history.append(ev)
                            if obs:
                                obs.update(output=_content_text(ev)[:4000])
                            continue
                        yield f"data: {json.dumps(_agui_payload(ev), ensure_ascii=False)}\n\n"
                except Exception as e:  # noqa: BLE001
                    err = {"type": "RUN_ERROR", "message": f"{type(e).__name__}: {e}",
                           "code": "server_error"}
                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
                finally:
                    if obs:
                        try:
                            from langfuse import get_client

                            get_client().flush()
                        except Exception:  # noqa: BLE001
                            pass

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


from fastapi.staticfiles import StaticFiles  # noqa: E402

# 兜底挂在 "/": 上面已注册的 API 路由优先匹配, 其余走静态目录(marked/purify 等)
app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
