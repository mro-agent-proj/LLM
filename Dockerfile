FROM ghcr.io/astral-sh/uv:0.12.17-python3.12-bookworm-slim
WORKDIR /service
ENV UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 STATE_DIR=/data
COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --frozen --no-dev
RUN useradd --uid 10001 --create-home worker && mkdir /data && chown worker /data
USER worker
ENTRYPOINT ["/service/.venv/bin/mro-worker"]
CMD ["run"]
