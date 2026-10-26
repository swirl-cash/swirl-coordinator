#!/usr/bin/env bash
# Fund an isolated payer and open a real regtest channel to the coordinator.
set -euo pipefail
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
  for _ in $(seq 1 60); do
    if ln_cli "$service" getinfo >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "$service did not start" >&2
  return 1
}

seed_liquidity() {
  echo "==> seeding coordinator payout liquidity with regtest coins"
  local label invoice
  label="swirl-liquidity-$(date +%s%N)"
  invoice=$(ln_cli lightningd invoice 2000000000 "$label" "regtest liquidity" | json_field bolt11)
  ln_cli payer pay "$invoice" >/dev/null
}

wait_for_node lightningd
wait_for_node payer

if ln_cli payer listpeerchannels | "${PYTHON}" -c '
import json, sys
channels = json.load(sys.stdin).get("channels", [])
raise SystemExit(0 if any(
    c.get("state") == "CHANNELD_NORMAL" for c in channels
) else 1)
'; then
  echo "==> regtest channel already active"
  seed_liquidity
  exit 0
fi

echo "==> mining regtest funds to the independent payer"
PAYER_ADDR=$(ln_cli payer newaddr | json_field bech32)
btc_cli generatetoaddress 101 "$PAYER_ADDR" >/dev/null

FUNDS_READY=""
for _ in $(seq 1 60); do
  if ln_cli payer listfunds | "${PYTHON}" -c '
import json, sys
raise SystemExit(0 if json.load(sys.stdin).get("outputs") else 1)
'; then
    FUNDS_READY=1
    break
  fi
  sleep 1
done
if [[ -z "$FUNDS_READY" ]]; then
  echo "payer funds were not confirmed" >&2
  ln_cli payer listfunds >&2
  exit 1
fi

echo "==> opening a 0.2 BTC payer -> coordinator channel"
COORD_ID=$(ln_cli lightningd getinfo | json_field id)
ln_cli payer connect "$COORD_ID@lightningd:19846" >/dev/null
ln_cli payer fundchannel "$COORD_ID" 20000000 >/dev/null
MINER_ADDR=$(ln_cli payer newaddr | json_field bech32)
btc_cli generatetoaddress 6 "$MINER_ADDR" >/dev/null

for _ in $(seq 1 60); do
  if ln_cli payer listpeerchannels | "${PYTHON}" -c '
import json, sys
channels = json.load(sys.stdin).get("channels", [])
raise SystemExit(0 if any(
    c.get("state") == "CHANNELD_NORMAL" for c in channels
) else 1)
'; then
    echo "==> regtest channel active"
    seed_liquidity
    exit 0
  fi
  sleep 1
done

echo "channel did not become active" >&2
ln_cli payer listpeers >&2
exit 1
