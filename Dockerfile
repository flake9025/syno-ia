# syntax=docker/dockerfile:1.7
#
# Image de syno-ia.
#
# Deux variantes se construisent depuis ce fichier :
#
#   WITH_LOCAL_LLM=false (défaut) — image légère (~1 Go). La génération passe par
#     Ollama, un service compatible OpenAI, ou le mode extractif intégré.
#
#   WITH_LOCAL_LLM=true — ajoute llama-cpp-python compilé **depuis les sources**.
#     C'est indispensable sur les NAS x86 d'ancienne génération (DS218+, DS718+,
#     DS918+… — Celeron Apollo Lake) : leur processeur ne possède pas AVX/AVX2 et
#     les roues précompilées de llama-cpp-python, bâties avec AVX2, provoquent un
#     « Illegal instruction » (SIGILL) au chargement du modèle.
#
ARG PYTHON_VERSION=3.11

# ---------------------------------------------------------------- base commune
FROM python:${PYTHON_VERSION}-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ------------------------------------------------------------------ build deps
FROM base AS builder

ARG WITH_LOCAL_LLM=false
ARG LLAMA_CPP_VERSION=0.3.16
ARG TARGETARCH

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake ninja-build git \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY requirements.txt ./
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r requirements.txt

# Compilation de llama.cpp sans instructions AVX sur x86 (compatibilité Apollo
# Lake), avec NEON conservé sur ARM64 où il est universellement disponible.
RUN if [ "${WITH_LOCAL_LLM}" = "true" ]; then \
      set -eux; \
      if [ "${TARGETARCH}" = "arm64" ]; then \
        FLAGS="-DGGML_NATIVE=OFF"; \
      else \
        FLAGS="-DGGML_NATIVE=OFF -DGGML_AVX=OFF -DGGML_AVX2=OFF -DGGML_AVX512=OFF -DGGML_FMA=OFF -DGGML_F16C=OFF"; \
      fi; \
      CMAKE_ARGS="${FLAGS} -DGGML_OPENMP=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=OFF" \
      FORCE_CMAKE=1 \
      pip install --no-binary :all: --verbose "llama-cpp-python==${LLAMA_CPP_VERSION}"; \
      pip install "huggingface-hub>=0.24"; \
    fi

# Supprime les artefacts de compilation inutiles à l'exécution.
RUN find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
 && find /opt/venv -name '*.pyc' -delete \
 && find /opt/venv -name 'tests' -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --------------------------------------------------------------------- runtime
FROM base AS runtime

ARG APP_VERSION=1.0.0
ARG BUILD_SHA=dev
ARG BUILD_DATE=""
ARG WITH_LOCAL_LLM=false

LABEL org.opencontainers.image.title="syno-ia" \
      org.opencontainers.image.description="RAG privé pour NAS Synology : authentification DSM et permissions respectées" \
      org.opencontainers.image.source="https://github.com/flake9025/syno-ia" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# libgomp1 : OpenMP, requis par onnxruntime et llama.cpp.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libgomp1 tini \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app
COPY app ./app
COPY web ./web

ENV APP_VERSION=${APP_VERSION} \
    BUILD_SHA=${BUILD_SHA} \
    BUILD_DATE=${BUILD_DATE} \
    WITH_LOCAL_LLM=${WITH_LOCAL_LLM} \
    PORT=8080 \
    DATA_DIR=/app/data \
    HF_HOME=/app/data/models/hf \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=2

RUN mkdir -p /app/data/models

VOLUME ["/app/data"]
EXPOSE 8080

# Le premier démarrage télécharge le modèle d'embeddings : période de grâce large.
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75 --no-server-header"]
