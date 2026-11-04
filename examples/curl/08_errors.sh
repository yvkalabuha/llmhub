#!/usr/bin/env bash
# Error objects: OpenAI "error" with code, reason, retryable, retry_after_s, task_id.
set -euo pipefail
: "${LLMHUB_URL:?}" "${LLMHUB_KEY:?}"
call() {  # prints body and HTTP status on the last line
  curl -sS -w '\n%{http_code}' "$@"
}
noauth=$(call "$LLMHUB_URL/v1/models")
echo "$noauth"
[ "$(tail -n1 <<<"$noauth")" = 401 ] && grep -q '"code":"unauthorized"' <<<"$noauth"
unknown=$(call "$LLMHUB_URL/v1/chat/completions" -H "Authorization: Bearer $LLMHUB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"no-such-model","messages":[{"role":"user","content":"x"}]}')
echo "$unknown"
[ "$(tail -n1 <<<"$unknown")" = 404 ] && grep -q '"code":"model_not_available"' <<<"$unknown"
bad=$(call "$LLMHUB_URL/v1/chat/completions" -H "Authorization: Bearer $LLMHUB_KEY" \
  -H "Content-Type: application/json" -H "X-LLMHub-Wait: 9999" \
  -d '{"model":"auto","messages":[{"role":"user","content":"x"}]}')
echo "$bad"
[ "$(tail -n1 <<<"$bad")" = 400 ] && grep -q '"code":"invalid_request"' <<<"$bad"
grep -q '"retryable":false' <<<"$bad"
