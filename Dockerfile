FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY steam_mcp/ steam_mcp/
RUN pip install -e .
CMD ["python", "-m", "steam_mcp.main"]
