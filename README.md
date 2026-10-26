# SWIRL Coordinator

Non-custodial, fixed-denomination mixing over Lightning — **software anyone can run**.
One coordinator is one node in the network; users can choose their coordinator.
Coordinators can exchange authenticated snapshots and independently derive a
shared anonymity-set root while keeping exits at each deposit's home node.

**Status: v0.6.0 — pre-production reference software. Do not use with real funds.**
57 automated tests cover the core protocol, federation rounds, held-HTLC
refund/recovery paths, official BIP340 vectors, and HTTP smoke flows through a
real uvicorn socket. The Compose smoke tests have passed against CLN v25.09 +
hold v0.3.3, including two coordinators with independent nodes, channels and
databases deriving one federation root and each completing a real regtest exit.
The SWIRL core pool uses a permanent append-only Merkle tree, 30-root history
and nullifier double-spend sets — see [docs/POOL.md](docs/POOL.md).
Held-HTLC entries are now implemented: the coordinator claims a deposit only
after paying its authorized exit, and can cancel unresolved entries so their
native Lightning timeout path returns funds. The remaining counterparty and
liveness boundaries are stated in [docs/REFUNDS.md](docs/REFUNDS.md).

## What is enforced in code (not in a README)

| Rule | Mechanism |
|---|---|
| Fixed denominations only | 0.001 / 0.01 / 0.1 / 1 BTC, everything else → `422 lattice_violation` |
| Receipt = key, not a database row | BIP340 Schnorr keypair generated on the user's device; coordinator stores only `sha256(pubkey ‖ amount)` |
| Fee and destination cannot be changed after signing | Payout authorization signs `(commitment, offer, amount, fee_bps)` together |
| No instant in-and-out | 60 min minimum hold + up to 15 min random jitter, per deposit |
| No address reuse | Every exit offer is single-use → `409 offer_reuse` |
| No double settlement | Atomic compare-and-set transitions and an fsync'd journal; only definite failures retry, ambiguous payments enter `PAYOUT_REVIEW` |
| Refundable entry | Optional held BOLT11 HTLCs; exit is paid before entry is claimed, with wall-clock + CLTV safety margins and explicit cancellation |
| Verifiable anonymity set | Permanent append-only Merkle tree (20 levels), 30-root history, inclusion proofs — the set never shrinks on settlement |
| Shared federation rounds | Authenticated peer snapshots, deterministic combined roots and cross-peer proofs; payouts remain at the home coordinator |
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
# Optional: exercise held-entry behavior with the in-memory mock.
export SWIRL_HELD_HTLC=true
export SWIRL_INVOICE_EXPIRY=7200  # must exceed hold + jitter + safety margin
uvicorn app.main:app --port 8080 --no-access-log   # API
python -m app.worker                               # payout worker, second shell
```

Without `SWIRL_CLN_RPC` the node uses a mock Lightning backend so the full
state machine can be exercised locally. Point `SWIRL_CLN_RPC` at a real CLN
socket for regtest. The default hold, jitter and settlement safety margins are
3600, 900 and 900 seconds; held mode rejects an invoice expiry that cannot
cover their sum.

## Asset backends

`SWIRL_ASSET_BACKEND=btc` is the default and preserves the existing CLN/mock
behavior. `SWIRL_ASSET_BACKEND=rgb` selects the explicit RGB adapter boundary,
which is **disabled**: `/v1/params` reports `enabled: false` and deposit
requests fail with `503 asset_backend_unavailable`. No RGB invoice or payout
is attempted.

One coordinator serves one asset. BTC and future RGB pools must use separate
databases, Merkle trees, nullifier sets and asset-specific denomination
configuration. See [docs/ASSETS.md](docs/ASSETS.md) for the interface and the
work required before RGB can be enabled.

## Run the full regtest stack (bitcoind + CLN + two coordinators)

```bash
docker compose up -d --build
bash scripts/regtest_smoke.sh        # single-coordinator settle + refund
bash scripts/regtest_federation.sh   # two coordinators, shared root, two exits
```

The smoke script funds an independent payer, opens a real channel and seeds
payout liquidity using regtest coins. It then walks keygen → held deposit →
pay on regtest → signed payout → fresh BOLT12 offer → hold + jitter → exit
payment → entry claim → Merkle proof, then repeats an entry and explicitly
cancels it to reach `REFUNDED`.

The federation script adds a second hold-enabled CLN node, coordinator and
SQLite database. It pays one deposit at each coordinator, checks identical
combined roots and cross-peer proofs, and settles one BOLT12 exit at each home
coordinator. Federation configuration and its exact trust boundary are in
[docs/FEDERATION.md](docs/FEDERATION.md). The CLN image pins hold plugin v0.3.3
and verifies its release-archive SHA-256. Both APIs bind to localhost only.

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
| GET | `/v1/params` | asset capabilities, lattice, fee, hold/jitter, expiry |
| POST | `/v1/mix/deposit` | `{amount_sats, pubkey}` → invoice + commitment + leaf_index + merkle_root |
| POST | `/v1/mix/payout` | `{commitment, root, nullifier_hash, pubkey, offer, fee_bps, signature}` → `release_at` |
| GET | `/v1/mix/deposit/{commitment}` | state, amounts, release time |
| GET | `/v1/mix/set` | permanent local/federated set size, root, scope and round id |
| GET | `/v1/mix/proof/{commitment}` | local or cross-peer Merkle inclusion proof |
| GET | `/v1/federation/snapshot` | authenticated local snapshot for configured peers |
| GET | `/v1/health` | liveness |

## Trust model & roadmap

- **Today**: the coordinator operates the HTLCs and can refuse service, but it
  cannot forge your payout terms (signature-bound) and does not hold your
  receipt key. Timing/amount correlation is reduced by the lattice, hold and
  jitter; a malicious coordinator can still observe its own logs. Held entries
  can be cancelled or left to their native CLTV timeout. Entry and exit are not
  atomic: the coordinator pays first and takes a bounded counterparty window.
  Entry preimages require database/backup protection. See
  [docs/REFUNDS.md](docs/REFUNDS.md) before any deployment.
- **Federation today**: peers share validated snapshots and derive common
  anonymity-set roots. They do not share nullifiers or perform atomic
  cross-coordinator exits; see [docs/FEDERATION.md](docs/FEDERATION.md).
- **Next**: signed coordinator identities and distributed exit authorization,
  PTLC/adaptor-signature privacy, and eventually BitVM-class trust
  minimization for large lanes.

## Tests

```bash
python -m unittest discover -s tests -v
bash -n scripts/regtest_setup.sh scripts/regtest_smoke.sh scripts/regtest_federation.sh
python -m compileall -q app tests
```

The 57 tests cover the lattice, official BIP340 vectors, authorization
binding, Merkle proofs, deterministic federation rounds and tamper rejection,
standard and held deposit→payout flows, cancellation and refunds, expiry
safety, ambiguous-payment review, journal recovery, old DB migration, backend
selection and fail-closed RGB behavior. Three HTTP smoke tests run through real
uvicorn sockets with the in-memory mock. They do not replace the real-CLN
Compose smoke tests.

## License

MIT — run it, fork it, audit it.
