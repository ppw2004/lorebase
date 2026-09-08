# 问答服务镜像: 双 venv(agentscope 主 + fastmcp MCP), 布局与仓库一致故零代码适配
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONUTF8=1 LANG=C.UTF-8

# 依赖层(缓存友好): 先装 requirements, 再拷源码
COPY agent/web/requirements-main.txt agent/web/requirements-mcp.txt /tmp/
RUN uv venv agent/.venv \
 && uv pip install --python agent/.venv/bin/python -r /tmp/requirements-main.txt \
 && uv venv agent/.venv-mcp \
 && uv pip install --python agent/.venv-mcp/bin/python -r /tmp/requirements-mcp.txt \
 && mkdir -p build

COPY docs/prompts/ docs/prompts/
COPY agent/agent.py agent/runner.py agent/
COPY agent/mcps/ agent/mcps/
COPY agent/web/ agent/web/

# 可选挂载: agent/domain-knowledge.md(领域知识), 密钥走环境变量
EXPOSE 8300
CMD ["agent/.venv/bin/python", "-m", "uvicorn", "agent.web.server:app", \
     "--host", "0.0.0.0", "--port", "8300"]
