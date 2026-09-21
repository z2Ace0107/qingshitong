FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QST_DB_PATH=/app/data/qingshitong.db \
    QST_APP_ENV=public_demo \
    QST_LLM_API_KEY_FILE=/run/secrets/qst_llm_api_key \
    QST_EMBEDDING_CACHE_DIR=/app/models/embeddings

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
# R0 Embedding runs on CPU. Install the matching official CPU wheel so the
# image does not pull the multi-hundred-megabyte CUDA runtime by default.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch==2.14.0+cpu" \
    && pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY web /app/web

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin qst \
    && mkdir -p /app/data /app/models/embeddings \
    && chown -R qst:qst /app

USER qst

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=3)"

ENTRYPOINT ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
