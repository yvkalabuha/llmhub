#!/usr/bin/env bash
# List the model aliases this client may use (plus "auto").
set -euo pipefail
: "${LLMHUB_URL:?}" "${LLMHUB_KEY:?}"
out=$(curl -sS --fail-with-body "$LLMHUB_URL/v1/models" -H "Authorization: Bearer $LLMHUB_KEY")
echo "$out"
grep -q '"object":"list"' <<<"$out"
