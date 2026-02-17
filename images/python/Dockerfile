FROM wowkmang:base

# Install Python 3
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-venv python3-dev && \
    rm -rf /var/lib/apt/lists/*

# Install uv
ENV UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
