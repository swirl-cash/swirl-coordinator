#!/usr/bin/env bash
# Real two-coordinator CLN regtest: shared set root, independent home exits.
# NO REAL FUNDS. Run after `docker compose up -d --build`.
set -euo pipefail
API_A="${API_A:-http://localhost:8080}"
API_B="${API_B:-http://localhost:8081}"
PYTHON="${PYTHON:-python3}"
cd "$(dirname "$0")/.."

ln_cli() {
  local service="$1"
  shift
  docker compose exec -T "$service" lightning-cli --network=regtest "$@"
}

btc_cli() {
  docker compose exec -T bitcoind bitcoin-cli -regtest \
    -rpcuser=swirl -rpcpassword=swirl "$@"
}

json_field() {
  "${PYTHON}" -c 'import sys,json;print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

wait_for_node() {
  local service="$1"
  for _ in $(seq 1 90); do
    if ln_cli "$service" getinfo >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "$service did not start" >&2
  return 1
}

wait_for_api() {
  local api="$1"
  for _ in $(seq 1 90); do
    if curl -sf "$api/v1/health" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "$api did not start" >&2
  return 1
}

channel_to_b_active() {
  ln_cli payer listpeerchannels | "${PYTHON}" -c '
import json, os, sys
channels = json.load(sys.stdin).get("channels", [])
peer = os.environ["COORD_B_ID"]
raise SystemExit(0 if any(
    c.get("peer_id") == peer and c.get("state") == "CHANNELD_NORMAL"
    for c in channels
) else 1)
'
}

seed_b_liquidity() {
  local label invoice
  label="swirl-liquidity-b-$(date +%s%N)"
  invoice=$(ln_cli lightningd-b invoice 2000000000 "$label" "regtest B liquidity" | json_field bolt11)
  ln_cli payer pay "$invoice" >/dev/null
}

setup_b_channel() {
  wait_for_node lightningd-b
  COORD_B_ID=$(ln_cli lightningd-b getinfo | json_field id)
  export COORD_B_ID
  ln_cli payer connect "$COORD_B_ID@lightningd-b:19846" >/dev/null || true
  if channel_to_b_active; then
    echo "==> payer -> coordinator B channel already active"
    seed_b_liquidity
    return
  fi

  echo "==> mining funds and opening payer -> coordinator B channel"
  PAYER_ADDR=$(ln_cli payer newaddr | json_field bech32)
  btc_cli generatetoaddress 101 "$PAYER_ADDR" >/dev/null
  for _ in $(seq 1 60); do
    if ln_cli payer listfunds | "${PYTHON}" -c '
import json, sys
raise SystemExit(0 if json.load(sys.stdin).get("outputs") else 1)
'; then
      break
    fi
    sleep 1
  done
  ln_cli payer connect "$COORD_B_ID@lightningd-b:19846" >/dev/null
  ln_cli payer fundchannel "$COORD_B_ID" 20000000 >/dev/null
  MINER_ADDR=$(ln_cli payer newaddr | json_field bech32)
  btc_cli generatetoaddress 6 "$MINER_ADDR" >/dev/null
  for _ in $(seq 1 90); do
    if channel_to_b_active; then
      echo "==> payer -> coordinator B channel active"
      seed_b_liquidity
      return
    fi
    sleep 1
  done
  echo "coordinator B channel did not become active" >&2
  ln_cli payer listpeerchannels >&2
  return 1
}

PAY_A_PID=""
PAY_B_PID=""
cleanup() {
  local pid
  for pid in "$PAY_A_PID" "$PAY_B_PID"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT

wait_for_state() {
  local api="$1" commitment="$2" expected="$3" state=""
  for _ in $(seq 1 60); do
    state=$(curl -sf "$api/v1/mix/deposit/$commitment" | json_field state)
    [[ "$state" == "$expected" ]] && return 0
    sleep 1
  done
  echo "expected $expected, got $state for $commitment at $api" >&2
  return 1
}

new_deposit() {
  local api="$1" keys dep
  keys=$("${PYTHON}" -m app.cli keygen --amount 1000000)
  RECEIPT=$(echo "$keys" | json_field receipt)
  COMMITMENT=$(echo "$keys" | json_field commitment)
  dep=$("${PYTHON}" -m app.cli deposit --receipt "$RECEIPT" --api "$api")
  INVOICE=$(echo "$dep" | json_field invoice)
}

echo "==> prepare existing coordinator A and independent coordinator B"
bash scripts/regtest_setup.sh
setup_b_channel
wait_for_api "$API_A"
wait_for_api "$API_B"

echo "==> create and really pay one held deposit at each coordinator"
new_deposit "$API_A"
RECEIPT_A="$RECEIPT"
COMMITMENT_A="$COMMITMENT"
INVOICE_A="$INVOICE"
new_deposit "$API_B"
RECEIPT_B="$RECEIPT"
COMMITMENT_B="$COMMITMENT"
INVOICE_B="$INVOICE"

ln_cli payer pay "$INVOICE_A" >/dev/null &
PAY_A_PID=$!
ln_cli payer pay "$INVOICE_B" >/dev/null &
PAY_B_PID=$!
wait_for_state "$API_A" "$COMMITMENT_A" HELD
wait_for_state "$API_B" "$COMMITMENT_B" HELD

echo "==> wait for both coordinators to derive the same federation root"
SHARED_ROOT=""
for _ in $(seq 1 60); do
  SET_A=$(curl -sf "$API_A/v1/mix/set")
  SET_B=$(curl -sf "$API_B/v1/mix/set")
  ROOT_A=$(echo "$SET_A" | json_field merkle_root)
  ROOT_B=$(echo "$SET_B" | json_field merkle_root)
  SCOPE_A=$(echo "$SET_A" | json_field scope)
  SCOPE_B=$(echo "$SET_B" | json_field scope)
  if [[ "$ROOT_A" == "$ROOT_B" && "$SCOPE_A" == federation && "$SCOPE_B" == federation ]] \
    && curl -sf "$API_A/v1/mix/proof/$COMMITMENT_B" >/dev/null \
    && curl -sf "$API_B/v1/mix/proof/$COMMITMENT_A" >/dev/null; then
    SHARED_ROOT="$ROOT_A"
    break
  fi
  sleep 1
done
if [[ -z "$SHARED_ROOT" ]]; then
  echo "coordinators did not converge on a shared set containing both deposits" >&2
  echo "$SET_A" | "${PYTHON}" -m json.tool >&2
  echo "$SET_B" | "${PYTHON}" -m json.tool >&2
  exit 1
fi
echo "$SET_A" | "${PYTHON}" -m json.tool

echo "==> authorize each home-coordinator exit against the shared root"
OFFER_A=$(ln_cli payer offer any swirl-federation-exit-a | json_field bolt12)
OFFER_B=$(ln_cli payer offer any swirl-federation-exit-b | json_field bolt12)
"${PYTHON}" -m app.cli payout --receipt "$RECEIPT_A" --commitment "$COMMITMENT_A" \
  --offer "$OFFER_A" --api "$API_A"
"${PYTHON}" -m app.cli payout --receipt "$RECEIPT_B" --commitment "$COMMITMENT_B" \
  --offer "$OFFER_B" --api "$API_B"

echo "==> wait for both real BOLT12 exits and held-entry claims"
sleep 90
wait_for_state "$API_A" "$COMMITMENT_A" SETTLED
wait_for_state "$API_B" "$COMMITMENT_B" SETTLED
wait "$PAY_A_PID"
PAY_A_PID=""
wait "$PAY_B_PID"
PAY_B_PID=""
"${PYTHON}" -m app.cli status --commitment "$COMMITMENT_A" --api "$API_A"
"${PYTHON}" -m app.cli status --commitment "$COMMITMENT_B" --api "$API_B"

echo "==> verify durable N-of-N nullifier commits on both databases"
for service in coordinator-api coordinator-api-b; do
  docker compose exec -T "$service" python -c '
import sqlite3
rows = sqlite3.connect("/data/swirl.db").execute(
    "SELECT state FROM federation_nullifiers"
).fetchall()
assert len(rows) >= 2, rows
assert all(state == "COMMITTED" for (state,) in rows), rows
print(f"nullifier commits verified: {len(rows)}")
'
done
echo "==> federation regtest passed: shared root $SHARED_ROOT"
