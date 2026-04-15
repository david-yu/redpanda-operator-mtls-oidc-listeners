#!/usr/bin/env bash
# Obtains an OIDC access token from Dex using the Resource Owner Password
# Credentials grant (direct username/password exchange).
#
# Prerequisites:
#   kubectl port-forward -n dex svc/dex 5556:5556
#
# Usage:
#   ./scripts/get-oidc-token.sh

set -euo pipefail

DEX_URL="${DEX_URL:-http://localhost:5556}"
CLIENT_ID="${CLIENT_ID:-redpanda}"
CLIENT_SECRET="${CLIENT_SECRET:-redpanda-secret}"
USERNAME="${USERNAME:-user@example.com}"
PASSWORD="${PASSWORD:-password}"

TOKEN_RESPONSE=$(curl -s -X POST "${DEX_URL}/dex/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" \
  -d "client_id=${CLIENT_ID}" \
  -d "client_secret=${CLIENT_SECRET}" \
  -d "username=${USERNAME}" \
  -d "password=${PASSWORD}" \
  -d "scope=openid email profile")

ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token')

if [ "$ACCESS_TOKEN" = "null" ] || [ -z "$ACCESS_TOKEN" ]; then
  echo "ERROR: Failed to obtain token. Response:" >&2
  echo "$TOKEN_RESPONSE" | jq . >&2
  exit 1
fi

echo "$ACCESS_TOKEN"
