#!/usr/bin/env bash
set -e
export $(grep -v '^#' .env | xargs)

AGENT_NAME="${1:?Usage: $0 <agent-name> [env] [app]}"
ENVIRONMENT="${2:-self-hosted}"
GOLEM_APP="${3:-seeta-ai-assistant:app}"
GOLEM_URL="${4:-golem.vadali.in}"

CMP_JSON=$(golem -E "$ENVIRONMENT" component list -F json | jq --arg app "$GOLEM_APP" '.[] | select(.componentName == $app)')
CMP_ID=$(echo "$CMP_JSON" | jq -r '.componentId')
REVISION=$(echo "$CMP_JSON" | jq -r '.componentRevision')

echo "Component: $CMP_ID  Revision: $REVISION"

golem -E "$ENVIRONMENT" agent delete "$AGENT_NAME" --yes

curl -sf -X POST \
  -H "Authorization: Bearer $GOLEM_STATIC_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"$AGENT_NAME\", \"args\": [], \"env\": {}}" \
  "https://$GOLEM_URL/v1/components/${CMP_ID}/workers"

golem -E "$ENVIRONMENT" agent update "$AGENT_NAME" automatic "$REVISION" --await --yes
echo "Done — $AGENT_NAME updated to revision $REVISION"
