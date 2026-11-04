#!/usr/bin/env bash
# llmhub-infer: build, scan and (only with `push`) publish to GHCR (T-372 E3, customer Q1).
#   nice -n 10 deploy/docker/publish-infer.sh scan   # build llmhub-infer:<ver>, save, scan
#   deploy/docker/publish-infer.sh push              # needs a clean scan of the SAME image id
# Rules:
# - the scan (scripts/image_secret_scan.py, every layer + config/history) must say
#   "verdict: clean" for the exact local image id, otherwise nothing is pushed;
# - the GHCR token is read from a 0600 file via --password-stdin, never argv/env/logs;
#   default $HOME/.config/llmhub/ghcr-token (override LLMHUB_GHCR_TOKEN_FILE);
# - login/push/logout use a throw-away DOCKER_CONFIG, so ~/.docker/config.json is untouched;
# - the pushed digest goes to dist/<ver>/infer.digest as ghcr.io/<owner>/llmhub-infer@sha256:...
#   (pool image in config.t372: always by digest). Visibility "public" is set by the owner.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
VER=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/src/llmhub/__init__.py")
[ -n "$VER" ] || { echo "publish: version not found" >&2; exit 2; }
OWNER=${LLMHUB_GHCR_OWNER:-yvkalabuha}
REPO=ghcr.io/$OWNER/llmhub-infer
LOCAL=llmhub-infer:$VER
OUT=${LLMHUB_DIST:-$ROOT/dist}/$VER
SCAN_DIR=${LLMHUB_SCAN_DIR:-${TMPDIR:-/tmp}}
TOKEN_FILE=${LLMHUB_GHCR_TOKEN_FILE:-$HOME/.config/llmhub/ghcr-token}
mkdir -p "$OUT"

log() { printf 'publish: %s\n' "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }
local_id() { docker image inspect -f '{{.Id}}' "$LOCAL" 2>/dev/null; }

do_scan() {
    if ! docker image inspect "$LOCAL" >/dev/null 2>&1; then
        log "building $LOCAL (cache only, --pull=false)"
        docker build --quiet --pull=false -t "$LOCAL" "$ROOT/infer-image" >/dev/null
    fi
    local id report
    id=$(local_id)
    TAR=$(mktemp "$SCAN_DIR/llmhub-infer-$VER.XXXXXX.tar")  # global: the EXIT trap needs it
    trap 'rm -f "$TAR"' EXIT  # also on die (exit), not only on return
    report="$OUT/infer.scan.txt"
    local tar=$TAR
    docker save "$LOCAL" -o "$tar"
    if python3 -I "$ROOT/scripts/image_secret_scan.py" "$tar" --report "$report.tmp" >/dev/null; then
        mv "$report.tmp" "$report"
    else
        mv "$report.tmp" "$report" 2>/dev/null || true
        die "scan is not clean (see $report: paths and rules only)"
    fi
    grep -qx 'verdict: clean' "$report" || die "scan report has no clean verdict"
    printf '%s\n' "$id" >"$OUT/infer.local-id"
    log "clean: $LOCAL $id ($(du -h "$tar" | cut -f1) saved)"
}

do_push() {
    [ -s "$OUT/infer.local-id" ] && grep -qx 'verdict: clean' "$OUT/infer.scan.txt" 2>/dev/null \
        || die "run '$0 scan' first"
    [ "$(local_id)" = "$(cat "$OUT/infer.local-id")" ] || die "$LOCAL changed since the scan"
    [ -f "$TOKEN_FILE" ] && [ ! -L "$TOKEN_FILE" ] || die "token file $TOKEN_FILE missing"
    [ "$(stat -c '%a' "$TOKEN_FILE")" = 600 ] || die "token file must be 0600"
    local out digest
    CFG=$(mktemp -d "${TMPDIR:-/tmp}/llmhub-ghcr.XXXXXX")  # global: the EXIT trap needs it
    trap 'DOCKER_CONFIG="$CFG" docker logout ghcr.io >/dev/null 2>&1 || true; rm -rf "$CFG"' EXIT
    local cfg=$CFG
    docker tag "$LOCAL" "$REPO:$VER"
    DOCKER_CONFIG="$cfg" docker login ghcr.io -u "$OWNER" --password-stdin <"$TOKEN_FILE" >/dev/null
    out=$(DOCKER_CONFIG="$cfg" docker push "$REPO:$VER")
    DOCKER_CONFIG="$cfg" docker logout ghcr.io >/dev/null
    digest=$(grep -o 'digest: sha256:[0-9a-f]\{64\}' <<<"$out" | tail -n1 | cut -d' ' -f2)
    [ -n "$digest" ] || die "no digest in the push output"
    printf '%s@%s\n' "$REPO" "$digest" >"$OUT/infer.digest"
    log "pushed $REPO:$VER -> $(cat "$OUT/infer.digest")"
    log "next: set the package public (owner), then check an anonymous pull by digest:"
    log "  DOCKER_CONFIG=\$(mktemp -d) docker pull $(cat "$OUT/infer.digest")"
}

case "${1:-}" in
    scan) do_scan ;;
    push) do_push ;;
    *) echo "usage: $0 scan|push" >&2; exit 2 ;;
esac
