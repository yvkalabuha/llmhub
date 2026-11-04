# llmhub control-plane image (T-371 §2.5): API, reaper and the weights validator.
# Built offline by deploy/docker/build.sh from a wheelhouse (hashes pinned by uv.lock);
# no secrets, configs or weights inside. Runs as 64100 (the reaper/api user); the weights
# validator overrides --user 64102. HEALTHCHECK is set by `docker run` (run-args/*.args).
FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY wheelhouse/ /wheels/
RUN pip install --no-index --find-links /wheels --require-hashes -r /wheels/requirements.txt \
    && pip install --no-index --no-deps /wheels/llmhub-*-py3-none-any.whl \
    && rm -rf /wheels

# render.py, gen_weights_cert.py and the templates (used by llmhub-deploy.sh via docker run)
COPY deploy/ /opt/llmhub/deploy/

LABEL org.opencontainers.image.title="llmhub" \
      org.opencontainers.image.description="llmhub control plane (ADR-0138)"
USER 64100
WORKDIR /tmp
CMD ["python", "-m", "llmhub", "--help"]
