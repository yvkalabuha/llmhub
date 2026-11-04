#!/usr/bin/env bash
# Synchronous chat completion. The server waits up to 480 s; use a client timeout above that.
set -euo pipefail
: "${LLMHUB_URL:?}" "${LLMHUB_KEY:?}"
MODEL="${LLMHUB_MODEL:-auto}"
out=$(curl -sS --fail-with-body --max-time 600 -D /dev/stderr \
  "$LLMHUB_URL/v1/chat/completions" \
  -H "Authorization: Bearer $LLMHUB_KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}]}")
echo "$out"
grep -q '"object":"chat.completion"' <<<"$out"
