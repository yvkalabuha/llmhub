#!/usr/bin/env bash
# Cancel a task: queued -> cancelled; running local -> cut; running cloud -> cancel_requested
# (GPU time already spent is paid). Finished tasks stay as they are.
set -euo pipefail
: "${LLMHUB_URL:?}" "${LLMHUB_KEY:?}"
MODEL="${LLMHUB_MODEL:-auto}"
AUTH=(-H "Authorization: Bearer $LLMHUB_KEY")
task=$(curl -sS --fail-with-body "$LLMHUB_URL/v1/tasks" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"long job $RANDOM\"}]}")
id=$(sed -n 's/.*"id":"\([^"]*\)".*/\1/p' <<<"$task")
out=$(curl -sS --fail-with-body -X POST "$LLMHUB_URL/v1/tasks/$id/cancel" "${AUTH[@]}")
echo "$out"
grep -q "\"id\":\"$id\"" <<<"$out"
grep -Eq '"status":"(cancelled|running|completed)"' <<<"$out"
