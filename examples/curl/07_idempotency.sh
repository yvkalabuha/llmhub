#!/usr/bin/env bash
# Idempotency-Key: a repeat returns the same task (200); the key with another body -> 409.
set -euo pipefail
: "${LLMHUB_URL:?}" "${LLMHUB_KEY:?}"
MODEL="${LLMHUB_MODEL:-auto}"
KEY="example-$(date +%s)-$RANDOM"
submit() {
  curl -sS "$LLMHUB_URL/v1/tasks" -H "Authorization: Bearer $LLMHUB_KEY" \
    -H "Content-Type: application/json" -H "Idempotency-Key: $KEY" -w '\n%{http_code}' \
    -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$1\"}]}"
}
first=$(submit "same body")
again=$(submit "same body")
other=$(submit "another body")
echo "$first"; echo "$again"; echo "$other"
id1=$(sed -n 's/.*"id":"\([^"]*\)".*/\1/p' <<<"$first")
id2=$(sed -n 's/.*"id":"\([^"]*\)".*/\1/p' <<<"$again")
[ "$(tail -n1 <<<"$first")" = 202 ]
[ "$(tail -n1 <<<"$again")" = 200 ] && [ "$id1" = "$id2" ]
[ "$(tail -n1 <<<"$other")" = 409 ]
grep -q '"code":"idempotency_conflict"' <<<"$other"
grep -q "\"task_id\":\"$id1\"" <<<"$other"
