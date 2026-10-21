FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /service
ENV UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1 STATE_DIR=/data
COPY pyproject.toml uv.lock ./
COPY app ./app
RUN uv sync --frozen --no-dev --extra openai
RUN useradd --uid 10001 --create-home worker && mkdir /data && chown worker /data
USER worker
EXPOSE 8100
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD /service/.venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health/ready', timeout=2)"
ENTRYPOINT ["/service/.venv/bin/mro-worker"]
CMD ["run"]
