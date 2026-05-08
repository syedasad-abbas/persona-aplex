# syntax=docker/dockerfile:1
# CPU image for personaplex-7b-v1 voice agent.
#
# Two services:
#   1. PersonaPlex moshi.server — real-time speech-to-speech (WebSocket :8998)
#   2. ESL inbound client       — connects to FreeSWITCH :8021, controls calls,
#      and uses uuid_audio_stream to pipe audio to/from PersonaPlex

FROM python:3.12-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    ca-certificates \
    build-essential \
    pkg-config \
    libopus-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---- Install PersonaPlex (moshi) from NVIDIA's repo ----
RUN git clone --depth 1 https://github.com/NVIDIA/personaplex.git /tmp/personaplex \
    && pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir /tmp/personaplex/moshi/ \
    && rm -rf /tmp/personaplex

# ---- Bridge dependencies ----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    # --- PersonaPlex server ---
    MOSHI_DEVICE=cpu \
    MOSHI_HOST=0.0.0.0 \
    MOSHI_PORT=8998 \
    VOICE_PROMPT=NATF2.pt \
    # --- FreeSWITCH ESL (inbound connection) ---
    FS_ESL_HOST=127.0.0.1 \
    FS_ESL_PORT=8021 \
    FS_ESL_PASSWORD=FS!Secure2026 \
    # --- Agent ---
    AGENT_DOMAIN=appointment \
    MAX_CALL_SECONDS=600 \
    # --- General ---
    LOG_LEVEL=INFO \
    HF_HOME=/app/cache/hf \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    OPENBLAS_NUM_THREADS=4 \
    NUMEXPR_NUM_THREADS=4

RUN mkdir -p /app/cache

# PersonaPlex WebSocket
EXPOSE 8998

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=3 \
  CMD python -c "import moshi; import pymysql; print('ok')" || exit 1

CMD ["python", "/app/app.py"]
