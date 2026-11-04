#!/usr/bin/env bash
# Asynchronous task: submit (202 + Location), poll the status, fetch the result.
set -euo pipefail
: "${LLMHUB_URL:?}" "${LLMHUB_KEY:?}"
MODEL="${LLMHUB_MODEL:-auto}"
AUTH=(-H "Authorization: Bearer $LLMHUB_KEY")
task=$(curl -sS --fail-with-body "$LLMHUB_URL/v1/tasks" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Summarize: llmhub\"}],
       \"llmhub\":{\"priority\":\"normal\",\"deadline_s\":3600}}")
echo "$task"
id=$(sed -n 's/.*"id":"\([^"]*\)".*/\1/p' <<<"$task")
for _ in $(seq 1 120); do
  status=$(curl -sS --fail-with-body "$LLMHUB_URL/v1/tasks/$id" "${AUTH[@]}")
  case "$status" in
    *'"status":"completed"'*|*'"status":"failed"'*|*'"status":"cancelled"'*) break ;;
  esac
  sleep 1  # real clients: sleep eta_s / Retry-After
done
echo "$status"
result=$(curl -sS --fail-with-body "$LLMHUB_URL/v1/tasks/$id/result" "${AUTH[@]}")
echo "$result"
grep -q '"object":"chat.completion"' <<<"$result"
