FROM python:3.12-slim-bookworm
ARG APP_UID=1000
ARG APP_GID=1000
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HOME=/tmp TOOLKIT_DATA_DIR=/data TOOLKIT_SECRETS_DIR=/run/toolkit-secrets \
    HF_HOME=/data/model-cache SENTENCE_TRANSFORMERS_HOME=/data/model-cache \
    ANONYMIZED_TELEMETRY=False TOKENIZERS_PARALLELISM=false OMP_NUM_THREADS=2
RUN apt-get update && apt-get install -y --no-install-recommends msmtp ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid ${APP_GID} toolkit \
    && useradd --uid ${APP_UID} --gid toolkit --no-create-home toolkit
WORKDIR /app
COPY requirements.txt requirements-runtime.txt ./
RUN pip install torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt
COPY hosting ./hosting
COPY ingest.py ./
USER toolkit
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/ready', timeout=8)"
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "hosting.container_runtime"]
