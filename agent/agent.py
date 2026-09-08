"""校验智能体入口（AgentScope 2.0.7）。

组装: MiniMax-M3(OpenAI 兼容) + 三 MCP(graph-read/graph-write/rag, stdio)
+ 已审批提示词(docs/prompts/) + 可选领域知识(agent/domain-knowledge.md)。
约束: 智能体不直连任何数据库; 提示词不硬编码(放 docs/prompts/, frontmatter approved 才可加载)。
"""
import os
import re
import sys
from pathlib import Path

from agentscope.middleware import TracingMiddleware

AGENT_DIR = Path(__file__).resolve().parent
ROOT = AGENT_DIR.parent
PROMPT_FILE = ROOT / "docs/prompts/node-validation.prompt.md"
AGENT_MD = AGENT_DIR / "domain-knowledge.md"
VENV_PY = AGENT_DIR / ".venv-mcp/bin/python"  # MCP 独立 venv(fastmcp 4 与 agentscope 的 mcp pin 冲突, 解耦)


def load_prompt() -> str:
    """加载已审批提示词正文(frontmatter 之后); 未审批直接拒载。"""
    raw = PROMPT_FILE.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if not m or "approved" not in m.group(1):
        raise RuntimeError(f"prompt not approved: {PROMPT_FILE}")
    return m.group(2)


def load_agent_md() -> str:
    """可选领域知识(领域硬事实, 校验与问答共用); 不存在返回空。"""
    if not AGENT_MD.exists():
        return ""
    s = AGENT_MD.read_text(encoding="utf-8")
    return s.split("## 运行日志")[0].strip()  # 运行日志节不进 system


def minimax_key():
    key = os.environ.get("MINIMAX_API_KEY")
    if not key:
        raise RuntimeError("MINIMAX_API_KEY not set (see .env.example)")
    return key


async def build_toolkit(names=None):
    """stateful MCP 须先 connect 再构造 Toolkit (agentscope 约束)。
    names: 需要的 MCP 子集, 默认全部 (如 ("graph-read", "rag") 只读问答用)。"""
    from agentscope.mcp import MCPClient, StdioMCPConfig
    from agentscope.tool import Toolkit

    def client(name, script):
        return MCPClient(name=name, is_stateful=True,
                         mcp_config=StdioMCPConfig(command=str(VENV_PY), args=[str(script)]))

    all_mcps = [("graph-read", "graph_read"), ("graph-write", "graph_write"), ("rag", "rag")]
    mcps = [client(n, AGENT_DIR / f"mcps/{m}/server.py")
            for n, m in all_mcps if names is None or n in names]
    for c in mcps:
        await c.connect()
    return Toolkit(mcps=mcps), mcps


class SafeTracingMiddleware(TracingMiddleware):
    """on_acting 层自定义: 框架自带的 async generator 包装与 MCP stdio 的
    anyio cancel scope 跨 task 冲突; 改用 langfuse SDK 的 tool observation
    (裸 OTel span 设 observation.type 属性服务端不认, A/B 实验实测)。
    reply/model 层沿用框架实现。"""

    async def on_acting(self, agent, input_kwargs, next_handler):
        import json as _json

        from langfuse import get_client

        tc = input_kwargs.get("tool_call")
        tname = getattr(tc, "name", "") or "tool"
        inp = getattr(tc, "input", None)
        inp_str = (_json.dumps(inp, ensure_ascii=False, default=str)[:2000]
                   if not isinstance(inp, str) else inp[:2000]) if inp is not None else ""
        obs_cm = get_client().start_as_current_observation(name=f"tool:{tname}", as_type="tool")
        with obs_cm as obs:
            obs.update(input=inp_str)
            chunks = []
            try:
                async for item in next_handler(**input_kwargs):
                    txt = getattr(item, "content", None)
                    if isinstance(txt, list):
                        txt = " ".join(b.get("text", "") for b in txt if isinstance(b, dict))
                    chunks.append(str(txt)[:400])
                    yield item
            finally:
                obs.update(output="\n".join(chunks)[:2000])


def build_agent(toolkit):
    from agentscope.agent import Agent, ReActConfig
    from agentscope.credential import OpenAICredential
    from agentscope.middleware import TracingMiddleware
    from agentscope.model import OpenAIChatModel

    model = OpenAIChatModel(
        credential=OpenAICredential(api_key=minimax_key(),
                                    base_url="https://api.minimaxi.com/v1"),
        model="MiniMax-M3",
        parameters=OpenAIChatModel.Parameters(max_tokens=65536),
        stream=False,
        context_size=1000000,
        max_retries=3,
    )
    system = (load_prompt()
              .replace("{{agent_md}}", load_agent_md() or "（无额外领域知识，仅依据词典/图谱/原文判断）")
              .replace("{{chunk_id}}", "（本次任务 chunk_id 见用户消息）"))
    from agentscope.permission import PermissionContext, PermissionMode
    from agentscope.state import AgentState
    state = AgentState(permission_context=PermissionContext(
        mode=PermissionMode.BYPASS))  # 无人值守; 写防护在 MCP 白名单+证据约束层
    return Agent(name="lorebase-validator", system_prompt=system, model=model,
                 toolkit=toolkit, state=state,
                 middlewares=[SafeTracingMiddleware()],  # reply/model 层 span; acting 层见类注释
                 react_config=ReActConfig(max_iters=50))


def _content_text(reply) -> str:
    """归一化回复内容为纯文本(块列表 -> 拼接 text 块)。"""
    c = reply.content
    if isinstance(c, str):
        return c
    parts = []
    for b in c or []:
        parts.append(b.get("text", "") if isinstance(b, dict) else (getattr(b, "text", "") or ""))
    return "\n".join(p for p in parts if p)


async def run_chunk(chunk_id: str, toolkit=None, mcps=None):
    """单 chunk 校验会话, 返回 (最终输出文本, usage dict)。
    toolkit/mcps 由调用方复用传入(每 chunk 新建+关闭 MCP 会污染 anyio
    cancel scope, 导致后续 chunk 的子进程启动被取消 — 2026-09-04 实测)。"""
    from agentscope.message import Msg

    own = False
    if toolkit is None:
        toolkit, mcps = await build_toolkit()
        own = True
    try:
        agent = build_agent(toolkit)
        reply = await agent.reply(Msg(name="user", role="user",
                                      content=[{"type": "text",
                                                "text": f"本次校验 chunk_id: {chunk_id} 。开始。"}]))
        usage = {}
        u = getattr(reply, "usage", None)
        if u:
            usage = dict(u) if not hasattr(u, "model_dump") else u.model_dump()
        return _content_text(reply), usage
    finally:
        if own:
            for c in mcps:
                try:
                    await c.close()
                except BaseException:
                    pass  # 清理路径吞一切(CancelledError 非 Exception 子类, 且不得掩盖主流程异常)


if __name__ == "__main__":
    import asyncio
    chunk_id = sys.argv[1]
    out, usage = asyncio.run(run_chunk(chunk_id))
    print(out)
    print("\n[usage]", usage, file=sys.stderr)
