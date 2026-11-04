#!/usr/bin/env bash
# Local release build (T-371 §2.5, I3). Workstation only; never on the server.
#   nice -n 10 deploy/docker/build.sh            -> dist/<ver>/
# Steps: wheel + `uv export` (hashes from uv.lock) -> wheelhouse via `pip download
# --only-binary :all: --require-hashes` inside the pinned python image -> `docker build
# --pull=false` -> `docker save | gzip` (llmhub + pinned nginx) -> image.id, nginx.id,
# deploy files, SHA256SUMS. The server loads the tarballs and never pulls (S3).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
# shellcheck disable=SC1091
. "$ROOT/deploy/docker/images.env"
VER=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/src/llmhub/__init__.py")
[ -n "$VER" ] || { echo "build: version not found" >&2; exit 2; }
OUT=${LLMHUB_DIST:-$ROOT/dist}/$VER
TAG=llmhub:$VER
NGINX_TAG=llmhub-nginx:${VER}
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/llmhub-build.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT

for img in "$PYTHON_IMAGE" "$NGINX_IMAGE"; do
    docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null
done

mkdir -p "$STAGE/wheelhouse" "$STAGE/deploy" "$OUT"
(cd "$ROOT" && uv build --wheel --out-dir "$STAGE/wheelhouse" >/dev/null)
(cd "$ROOT" && uv export --frozen --no-dev --no-emit-project --format requirements-txt \
    --output-file "$STAGE/wheelhouse/requirements.txt" >/dev/null)
docker run --rm --name "llmhub-build-dl-$$" --label llmhub-build=1 \
    --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$STAGE/wheelhouse:/w" "$PYTHON_IMAGE" \
    pip download --quiet --no-cache-dir --disable-pip-version-check --only-binary :all: --require-hashes \
    -r /w/requirements.txt -d /w

cp "$ROOT/deploy/render.py" "$ROOT/deploy/gen_weights_cert.py" \
   "$ROOT/deploy/config.example.yaml" "$ROOT/deploy/nginx-weights.conf" "$STAGE/deploy/"
cp "$ROOT/deploy/docker/Dockerfile" "$ROOT/deploy/docker/.dockerignore" "$STAGE/"
docker build --quiet --pull=false --label "org.llmhub.release=$VER" -t "$TAG" "$STAGE" >/dev/null

docker tag "$NGINX_IMAGE" "$NGINX_TAG"
docker image inspect -f '{{.Id}}' "$TAG" >"$OUT/image.id"
docker image inspect -f '{{.Id}}' "$NGINX_TAG" >"$OUT/nginx.id"
docker save "$TAG" | gzip -n >"$OUT/llmhub-$VER.tar.gz"
docker save "$NGINX_TAG" | gzip -n >"$OUT/nginx-weights-$VER.tar.gz"
echo "$VER" >"$OUT/VERSION"
rm -rf "$OUT/run-args" "$OUT/systemd"
cp -r "$ROOT/deploy/docker/run-args" "$ROOT/deploy/systemd" "$OUT/"
cp "$ROOT/deploy/server/llmhub-deploy.sh" "$ROOT/deploy/logrotate-llmhub-acct" \
   "$ROOT/scripts/e2e_mock.py" "$OUT/"
(cd "$OUT" && rm -f SHA256SUMS && find . -type f -printf '%P\n' | LC_ALL=C sort \
    | xargs sha256sum >"$STAGE/SHA256SUMS" && mv "$STAGE/SHA256SUMS" SHA256SUMS)
echo "build: $OUT"
echo "image $(cat "$OUT/image.id") $(du -h "$OUT/llmhub-$VER.tar.gz" | cut -f1)"
