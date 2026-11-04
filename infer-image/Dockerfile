# llmhub-infer: llama.cpp server (CUDA 12.8) + pod runtime (weights loader, front, supervisor).
# No weights, no secrets, no certificates: everything arrives via env at start (T-370 plan, 2a).
# Base pinned by digest (official image, Ubuntu 24.04, /app/llama-server, CUDA_VERSION=12.8.1).
ARG LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server-cuda12-b12063@sha256:d5ffba7d096ab0236c65dc147d797b4b7658a7e5425634593373635d661bccda
# The ARG value is tag + digest; hadolint cannot see through the variable.
# hadolint ignore=DL3006
FROM ${LLAMA_IMAGE}

# Python pinned to the Ubuntu 24.04 security-pocket version seen on 2026-11-04. The archive drops
# superseded versions, so a rebuild months later needs these two pins refreshed
# (apt-cache policy python3.12); a stale pin fails the build loudly instead of drifting silently.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3.12=3.12.3-1ubuntu0.17 python3.12-venv=3.12.3-1ubuntu0.17 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3.12 -m venv /opt/llmhub/venv \
    && /opt/llmhub/venv/bin/pip install --require-hashes --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY llmhub_infer /opt/llmhub/llmhub_infer

ENV PYTHONPATH=/opt/llmhub PYTHONUNBUFFERED=1 LLMHUB_WEIGHTS_DIR=/models

# The base image ships a curl /health check without the API key; it would report the pod unhealthy.
HEALTHCHECK NONE
EXPOSE 8443 8080
ENTRYPOINT ["/opt/llmhub/venv/bin/python", "-m", "llmhub_infer"]
