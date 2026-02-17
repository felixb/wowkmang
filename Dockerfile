FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
ENV UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

COPY wowkmang/ ./wowkmang/

EXPOSE 8484

CMD ["uv", "run", "uvicorn", "wowkmang.api:app", "--host", "0.0.0.0", "--port", "8484"]
