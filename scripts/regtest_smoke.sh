#!/usr/bin/env bash
# End-to-end regtest smoke test. Run AFTER `docker compose up -d` is healthy.
# Uses the API on localhost:8080 with shortened holds (60s + jitter).
set -euo pipefail
API="${API:-http://localhost:8080}"
PYTHON="${PYTHON:-python3}"
cd "$(dirname "$0")/.."
bash scripts/regtest_setup.sh

echo "==> params"
curl -sf "$API/v1/params" | "${PYTHON}" -m json.tool

PAY_PID=""
cleanup() {
  if [[ -n "$PAY_PID" ]] && kill -0 "$PAY_PID" 2>/dev/null; then
    kill "$PAY_PID" 2>/dev/null || true
    wait "$PAY_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

json_field() {
  "${PYTHON}" -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

wait_for_state() {
  local commitment="$1" expected="$2" state=""
  for _ in $(seq 1 40); do
    state=$(curl -sf "$API/v1/mix/deposit/$commitment" | json_field state)
    [[ "$state" == "$expected" ]] && return 0
    sleep 1
  done
  echo "expected $expected, got $state for $commitment" >&2
  return 1
}

new_deposit() {
  local keys dep
  keys=$("${PYTHON}" -m app.cli keygen --amount 1000000)
  RECEIPT=$(echo "$keys" | json_field receipt)
  COMMITMENT=$(echo "$keys" | json_field commitment)
  dep=$("${PYTHON}" -m app.cli deposit --receipt "$RECEIPT" --api "$API")
  echo "$dep"
  INVOICE=$(echo "$dep" | json_field invoice)
  PAYMENT_HASH=$(echo "$dep" | json_field payment_hash)
}

echo "==> held-HTLC settlement (0.01 BTC quantum)"
new_deposit

echo "==> pay in the background; a held invoice waits for settlement"
docker compose exec -T payer lightning-cli --network=regtest pay "$INVOICE" &
PAY_PID=$!
wait_for_state "$COMMITMENT" HELD

echo "==> create a fresh BOLT12 offer and authorize the exit"
OFFER=$(docker compose exec -T payer lightning-cli --network=regtest offer any swirl-exit | json_field bolt12)
"${PYTHON}" -m app.cli payout --receipt "$RECEIPT" --commitment "$COMMITMENT" --offer "$OFFER" --api "$API"

echo "==> waiting for min-hold + jitter, exit payment, then entry claim"
sleep 90
wait_for_state "$COMMITMENT" SETTLED
wait "$PAY_PID"
PAY_PID=""
"${PYTHON}" -m app.cli status --commitment "$COMMITMENT" --api "$API"

echo "==> explicit cancellation returns a second held entry"
new_deposit
docker compose exec -T payer lightning-cli --network=regtest pay "$INVOICE" &
PAY_PID=$!
wait_for_state "$COMMITMENT" HELD
docker compose exec -T lightningd lightning-cli --network=regtest cancelholdinvoice "$PAYMENT_HASH"
wait "$PAY_PID" || true
PAY_PID=""
wait_for_state "$COMMITMENT" REFUNDED
"${PYTHON}" -m app.cli status --commitment "$COMMITMENT" --api "$API"

echo "==> anonymity set + inclusion proof"
curl -sf "$API/v1/mix/set" | "${PYTHON}" -m json.tool
curl -sf "$API/v1/mix/proof/$COMMITMENT" | "${PYTHON}" -m json.tool
