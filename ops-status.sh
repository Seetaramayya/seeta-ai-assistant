#!/usr/bin/env bash
set -euo pipefail

export $(grep -v '^#' .env | xargs)

WORKERS="https://golem.vadali.in/v1/components/019e3fe7-e92c-7e52-899c-f79b3b04408c/workers"
AUTH=(-H "Authorization: Bearer $GOLEM_STATIC_TOKEN")

echo "=== Seeta AI Assistant Agent Status ==="
curl -s "${AUTH[@]}" "$WORKERS" \
  | jq '.workers[] | {agent: .agentId.agentId, status: .status, memoryMB: (.totalLinearMemorySize / 1048576 | round), created: .createdAt}'

echo ""
echo "=== Summary ==="
curl -s "${AUTH[@]}" "$WORKERS" | jq '
  .workers
  | {
      total:        length,
      users:        [.[] | select(.agentId.agentId | startswith("UserAgent"))]        | length,
      shards:       [.[] | select(.agentId.agentId | startswith("DirectoryShardAgent"))] | length,
      running:      [.[] | select(.status == "Running")]  | length,
      idle:         [.[] | select(.status == "Idle")]     | length,
      failed:       [.[] | select(.status == "Failed")]   | length
    }'
