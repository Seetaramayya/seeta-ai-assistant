#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-self-hosted}"

case "$TARGET" in
  local)         ENV="local" ;;
  golem-cloud|cloud) ENV="cloud" ;;
  self-hosted|self-hosting) ENV="self-hosted" ;;
  *)
    echo "Usage: $0 [local|golem-cloud|self-hosted]" >&2
    exit 1
    ;;
esac

echo "Deleting all agents in: $ENV"

golem -E "$ENV" agent list | \
  grep '┆' | grep -v 'Component name' | \
  awk -F'┆' '{gsub(/^[[:space:]]+|[[:space:]]+$/, "", $3); print $3}' | \
  grep -v '^$' | \
  while read -r agent; do
    echo "Deleting: $agent"
    golem -E "$ENV" agent delete "$agent"
  done

echo "Done."
