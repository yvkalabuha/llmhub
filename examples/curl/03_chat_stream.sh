#!/usr/bin/env bash
# SSE stream. v1: ": keep-alive" comments while the task waits/runs, the answer chunks at the end.
set -euo pipefail
: "${LLMHUB_URL:?}" "${LLMHUB_KEY:?}"
MODEL="${LLMHUB_MODEL:-auto}"
out=$(curl -sS --fail-with-body -N --max-time 600 "$LLMHUB_URL/v1/chat/completions" \
  -H "Authorization: Bearer $LLMHUB_KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"stream\":true,\"stream_options\":{\"include_usage\":true},
       \"messages\":[{\"role\":\"user\",\"content\":\"Say hello\"}]}")
echo "$out"
grep -q '"object":"chat.completion.chunk"' <<<"$out"
grep -q '^data: \[DONE\]' <<<"$out"
