FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, so a source edit does not invalidate the installed wheels.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY web ./web
COPY samples ./samples
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    NOTCHGEN_DATA=/data \
    NOTCHGEN_WEB=/app/web

RUN useradd --uid 10001 --create-home app && mkdir -p /data && chown -R app /data /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request as r; r.urlopen('http://127.0.0.1:8000/healthz').read()"]

CMD ["uvicorn", "notchgen.api:app", "--host", "0.0.0.0", "--port", "8000"]
