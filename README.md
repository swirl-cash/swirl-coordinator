# SWIRL Coordinator

Non-custodial, fixed-denomination mixing over Lightning — **software anyone can run**.
One coordinator is one node in the network; users can choose their coordinator,
and federated settlement (shared anonymity sets) is the next milestone.

**Status: v0.3.0 — regtest software. Do not use with real funds.**
Core protocol logic is unit-tested (30 tests, including the official BIP340
vectors); the CLN integration is written against the real JSON-RPC API and is
exercised through the regtest stack below.
The pool model follows tornado-core: permanent append-only Merkle tree,
30-root history, nullifier double-spend sets — see
[docs/TORNADO.md](docs/TORNADO.md) for the exact mapping.
**Known trust gap:** once a deposit HTLC settles, a vanished coordinator
cannot be forced to refund. The held-HTLC entry design that closes this is
specified in [docs/REFUNDS.md](docs/REFUNDS.md) and is the next milestone.

## What is enforced in code (not in a README)

| Rule | Mechanism |
|---|---|
| Fixed denominations only | 0.001 / 0.01 / 0.1 / 1 BTC, everything else → `422 lattice_violation` |
| Receipt = key, not a database row | BIP340 Schnorr keypair generated on the user's device; coordinator stores only `sha256(pubkey ‖ amount)` |
| Fee and destination cannot be changed after signing | Payout authorization signs `(commitment, offer, amount, fee_bps)` together |
| No instant in-and-out | 60 min minimum hold + up to 15 min random jitter, per deposit |
| No address reuse | Every exit offer is single-use → `409 offer_reuse` |
| No double settlement | Atomic compare-and-set state transitions, `SETTLING` guard, retry on failure |
| Verifiable anonymity set | Permanent append-only Merkle tree (20 levels), 30-root history, inclusion proofs — the set never shrinks on settlement |
| Double-spend protection | Nullifier set, burned atomically with the payout queue — `409 already_spent` is final |
| Destination hygiene | Plaintext offers are purged from the DB on settlement; secret keys are never transmitted or stored |
| Crash-safe settlement | Every payment is appended to an fsync'd journal before the state flip; SETTLING rows reconcile automatically |
| Real payment timestamps | Holds start at the node's `paid_at`, not the poller's clock |
| Atomic offer burn + queue | `BEGIN IMMEDIATE` transaction — a failed queue never burns the user's offer |
| Hardened CLN client | Incremental JSON framing, notification skipping, reconnect on idempotent reads only — payments are never blindly retried |
| Edge protection | 32 KB body cap, per-IP sliding-window rate limits (30 writes / 120 reads per minute) |

**Crypto backend:** install `coincurve` (`pip install -r requirements-prod.txt`)
and receipt signatures switch to the audited, constant-time libsecp256k1.
The pure-Python fallback is for regtest/reference only.

**Process model:** exactly one API process + one payout worker per SQLite
file. Multi-process writes are serialized with `BEGIN IMMEDIATE` and
`busy_timeout`, but do NOT horizontally scale uvicorn workers — the rate
limiter and the operational model assume a single API process.

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
| POST | `/v1/mix/deposit` | `{amount_sats, pubkey}` → invoice + commitment + leaf_index + merkle_root |
| POST | `/v1/mix/payout` | `{commitment, root, nullifier_hash, pubkey, offer, fee_bps, signature}` → `release_at` |
| GET | `/v1/mix/deposit/{commitment}` | state, amounts, release time |
| GET | `/v1/mix/set` | live anonymity-set size + Merkle root |
| GET | `/v1/mix/proof/{commitment}` | Merkle inclusion proof |
| GET | `/v1/health` | liveness |

## Trust model & roadmap

- **Today**: the coordinator operates the HTLCs and can refuse service, but it
  cannot forge your payout terms (signature-bound) and does not hold your
  receipt key. Timing/amount correlation is reduced by the lattice, the hold
  and jitter — a malicious coordinator can still observe its own logs, so run
  your own or choose one you trust. **A settled deposit is not yet
  force-refundable if the operator vanishes; see [docs/REFUNDS.md](docs/REFUNDS.md)
  for the held-HTLC design that removes this assumption.**
- **Next**: held-HTLC entries (timeout = automatic depositor refund), then
  federated settlement rounds (merge commitment trees across
  coordinators → one shared anonymity set), PTLC/adaptor-signature privacy,
  and eventually BitVM-class trust minimization for large lanes.

## Tests

```bash
python -m unittest discover -s tests -v
```

Covers the lattice, official BIP340 sign/verify vectors, receipt and
authorization binding, Merkle proofs, the full deposit→hold→payout state
machine, offer reuse, fee tampering, invoice expiry, failed-payout retry,
double-settlement guards, atomic offer queueing, journal-based crash
recovery and real-payment-time holds.

## License

MIT — run it, fork it, audit it.
