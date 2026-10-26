#!/usr/bin/env bash
# End-to-end regtest smoke test. Run AFTER `docker compose up -d` is healthy.
# Uses the API on localhost:8080 with shortened holds (60s + jitter).
set -euo pipefail
API="${API:-http://localhost:8080}"
cd "$(dirname "$0")/.."

echo "==> params"
curl -sf "$API/v1/params" | python -m json.tool

echo "==> keygen (0.01 BTC quantum)"
KEYS=$(python -m app.cli keygen --amount 1000000)
echo "$KEYS"
RECEIPT=$(echo "$KEYS" | python -c 'import sys,json;print(json.load(sys.stdin)["receipt"])')
COMMITMENT=$(echo "$KEYS" | python -c 'import sys,json;print(json.load(sys.stdin)["commitment"])')

echo "==> deposit"
DEP=$(python -m app.cli deposit --receipt "$RECEIPT" --api "$API")
echo "$DEP"
INVOICE=$(echo "$DEP" | python -c 'import sys,json;print(json.load(sys.stdin)["invoice"])')

echo "==> pay the invoice from the regtest CLN node"
docker compose exec -T lightningd lightning-cli --network=regtest pay "$INVOICE"
sleep 8

echo "==> status (expect HELD)"
python -m app.cli status --commitment "$COMMITMENT" --api "$API"

echo "==> create a fresh BOLT12 offer for the exit"
OFFER=$(docker compose exec -T lightningd lightning-cli --network=regtest offer any swirl-exit | python -c 'import sys,json;print(json.load(sys.stdin)["bolt12"])')

echo "==> authorize payout (signed locally, key never leaves this machine)"
python -m app.cli payout --receipt "$RECEIPT" --commitment "$COMMITMENT" --offer "$OFFER" --api "$API"

echo "==> waiting for min-hold + jitter"
sleep 90
python -m app.cli status --commitment "$COMMITMENT" --api "$API"

echo "==> anonymity set + inclusion proof"
curl -sf "$API/v1/mix/set" | python -m json.tool
curl -sf "$API/v1/mix/proof/$COMMITMENT" | python -m json.tool || true
