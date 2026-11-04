#!/usr/bin/env bash
# Operator view (admin key only): status, budget, Prometheus metrics.
set -euo pipefail
: "${LLMHUB_URL:?}" "${LLMHUB_ADMIN_KEY:?}"
AUTH=(-H "Authorization: Bearer $LLMHUB_ADMIN_KEY")
status=$(curl -sS --fail-with-body "$LLMHUB_URL/admin/status" "${AUTH[@]}")
echo "$status"
grep -q '"pools"' <<<"$status"
budget=$(curl -sS --fail-with-body "$LLMHUB_URL/admin/budget" "${AUTH[@]}")
echo "$budget"
grep -q '"stage"' <<<"$budget"
metrics=$(curl -sS --fail-with-body "$LLMHUB_URL/metrics" "${AUTH[@]}")
grep -q '^llmhub_halted' <<<"$metrics"
curl -sS --fail-with-body "$LLMHUB_URL/healthz"
