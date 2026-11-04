#!/usr/bin/env bash
# Explicit lease: keeps the cloud GPU while you renew it (heartbeat < ttl_s), then close.
# Needs a client with cloud_allowed and a model that has a cloud pool.
set -euo pipefail
: "${LLMHUB_URL:?}" "${LLMHUB_KEY:?}"
MODEL="${LLMHUB_LEASE_MODEL:-big}"
AUTH=(-H "Authorization: Bearer $LLMHUB_KEY")
lease=$(curl -sS --fail-with-body "$LLMHUB_URL/v1/leases" "${AUTH[@]}" \
  -H "Content-Type: application/json" -d "{\"model\":\"$MODEL\",\"ttl_s\":120}")
echo "$lease"
id=$(sed -n 's/.*"id":"\([^"]*\)".*/\1/p' <<<"$lease")
renew=$(curl -sS --fail-with-body -X POST "$LLMHUB_URL/v1/leases/$id/renew" "${AUTH[@]}")
echo "$renew"
grep -q '"expires_at"' <<<"$renew"
closed=$(curl -sS --fail-with-body "$LLMHUB_URL/v1/leases/$id/close" "${AUTH[@]}" \
  -H "Content-Type: application/json" -d '{"final":true}')
echo "$closed"
grep -q '"closed":true' <<<"$closed"
