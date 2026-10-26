# SWIRL Coordinator

Non-custodial, fixed-denomination mixing over Lightning — **software anyone can run**.
One coordinator is one node in the network; users can choose their coordinator,
and federated settlement (shared anonymity sets) is the next milestone.

**Status: v0.1.0 — regtest software. Do not use with real funds.**
Core protocol logic is unit-tested (21 tests, including the official BIP340
vectors); the CLN integration is written against the real JSON-RPC API and is
exercised through the regtest stack below.

## What is enforced in code (not in a README)

| Rule | Mechanism |
|---|---|
| Fixed denominations only | 0.001 / 0.01 / 0.1 / 1 BTC, everything else → `422 lattice_violation` |
| Receipt = key, not a database row | BIP340 Schnorr keypair generated on the user's device; coordinator stores only `sha256(pubkey ‖ amount)` |
| Fee and destination cannot be changed after signing | Payout authorization signs `(commitment, offer, amount, fee_bps)` together |
| No instant in-and-out | 60 min minimum hold + up to 15 min random jitter, per deposit |
| No address reuse | Every exit offer is single-use → `409 offer_reuse` |
| No double settlement | Atomic compare-and-set state transitions, `SETTLING` guard, retry on failure |
| Verifiable anonymity set | Merkle root + inclusion proofs of all live commitments |
| Destination hygiene | Plaintext offers are purged from the DB on settlement; secret keys are never transmitted or stored |

## Run a coordinator (development, no Lightning needed)

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8080 --no-access-log   # API
python -m app.worker                               # payout worker, second shell
```

Without `SWIRL_CLN_RPC` the node uses a mock Lightning backend so the full
state machine can be exercised locally. Point `SWIRL_CLN_RPC` at a real CLN
socket to go live on regtest.

## Run the full regtest stack (bitcoind + CLN + coordinator)

```bash
docker compose up -d --build
bash scripts/regtest_smoke.sh
```

The smoke test walks the entire flow: keygen → deposit → pay the invoice on
regtest → sign a payout authorization → fresh BOLT12 offer → hold + jitter →
settlement → Merkle inclusion proof.

## Use it as a user

```bash
# 1. create a receipt for one quantum (0.01 BTC = 1,000,000 sats)
python -m app.cli keygen --amount 1000000
#    -> receipt (KEEP SECRET), pubkey, commitment

# 2. deposit: send only amount + pubkey, get a Lightning invoice
python -m app.cli deposit --receipt SWIRL1-... --api http://localhost:8080

# 3. after payment + the hold, sign for a fresh BOLT12 offer and queue the exit
python -m app.cli payout --receipt SWIRL1-... --commitment <hex> --offer lno1... --api ...

# 4. track it
python -m app.cli status --commitment <hex> --api ...
```

The receipt secret key **never leaves your machine** — only the pubkey and a
Schnorr signature are sent. Lose the receipt and nobody (including the
coordinator) can authorize your exit.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/params` | lattice, fee, hold/jitter, expiry |
| POST | `/v1/mix/deposit` | `{amount_sats, pubkey}` → invoice + commitment |
| POST | `/v1/mix/payout` | `{commitment, pubkey, offer, fee_bps, signature}` → `release_at` |
| GET | `/v1/mix/deposit/{commitment}` | state, amounts, release time |
| GET | `/v1/mix/set` | live anonymity-set size + Merkle root |
| GET | `/v1/mix/proof/{commitment}` | Merkle inclusion proof |
| GET | `/v1/health` | liveness |

## Trust model & roadmap

- **Today**: the coordinator operates the HTLCs and can refuse service, but it
  cannot forge your payout terms (signature-bound) and does not hold your
  receipt key. Timing/amount correlation is reduced by the lattice, the hold
  and jitter — a malicious coordinator can still observe its own logs, so run
  your own or choose one you trust. That is exactly why this is open source.
- **Next**: federated settlement rounds (merge commitment trees across
  coordinators → one shared anonymity set), PTLC/adaptor-signature privacy,
  timelocked refund paths, and eventually BitVM-class trust minimization for
  large lanes.

## Tests

```bash
python -m unittest discover -s tests -v
```

Covers the lattice, official BIP340 sign/verify vectors, receipt and
authorization binding, Merkle proofs, the full deposit→hold→payout state
machine, offer reuse, fee tampering, invoice expiry, failed-payout retry and
double-settlement guards.

## License

MIT — run it, fork it, audit it.
