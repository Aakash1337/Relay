# RELAY application image. One image serves every process: the API is the
# default command, and the workers are the same image with a different
# command (relay-worker / relay-events / relay-retention / relay-migrate).
#
#   docker build -t relay .
#   docker run --rm -p 8000:8000 --env-file .env relay
#
# Configuration comes from the environment (--env-file or compose env_file);
# nothing is baked in. See docker-compose.prod.yml for the full topology.

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code edits don't invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN useradd --system --create-home relay && chown -R relay:relay /app
USER relay

EXPOSE 8000

CMD ["uvicorn", "relay.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
