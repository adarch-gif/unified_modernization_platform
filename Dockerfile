FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY examples ./examples

RUN python -m pip install --upgrade pip \
    && python -m pip install ".[gcp,telemetry]"

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "unified_modernization.gateway.http_api:app", "--host", "0.0.0.0", "--port", "8080"]
