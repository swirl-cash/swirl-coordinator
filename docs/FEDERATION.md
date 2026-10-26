# Federated rounds and N-of-N nullifier authorization

Implemented in v0.7.0. Coordinators share anonymity sets and prevent the same
nullifier from being queued at two home coordinators. Exits still execute only
at the deposit's home coordinator; this is not an atomic cross-coordinator
payment protocol.

## Round construction

1. Every coordinator publishes its complete append-only local commitment list
   and local Merkle root at `GET /v1/federation/snapshot`.
2. The worker downloads peer snapshots over authenticated HTTPS. A snapshot is
   accepted only after its coordinator id, leaf encoding, uniqueness, size and
   recomputed local Merkle root have been validated.
3. Snapshots are ordered by `coordinator_id`; local leaf order is preserved.
   A commitment appearing in multiple snapshots is inserted only once.
4. Each coordinator independently rebuilds the same combined append-only tree.
   The `round_id` binds the ordered coordinator ids and their local roots.
5. The combined root enters the rolling 30-root history. Inclusion proofs can
   cover local and remote commitments.

If a peer is unavailable or returns an invalid snapshot, the last valid stored
snapshot remains in use. Until at least two valid snapshots exist, the API
falls back to the local set. Different nodes can temporarily expose different
rounds while new deposits propagate.

**Federated payout authorization currently accepts only the current shared
root.** The implementation generates proofs against the current combined tree;
it does not yet retain historical combined trees for old-root proofs. Standalone
pools retain their normal recent-root behavior.

## N-of-N spend authorization

A home coordinator must obtain a durable reservation from itself and **every
configured peer** before queueing an exit. This is N-of-N, not an arbitrary
threshold k/n policy.

1. The home derives the current-root Merkle proof and the exact authorization
   digest binding commitment, root, nullifier, offer, amount and fee.
2. The home durably reserves the nullifier in its own federation table.
3. Each peer independently validates the signature, denomination, fee policy,
   current shared root and Merkle proof, then atomically reserves the nullifier.
4. A conflicting reservation or spent nullifier rejects the spend. Any offline
   peer, invalid response or other failure prevents queueing. Already attempted
   peers and the home receive an abort, including a peer whose reserve response
   was lost.
5. Only after every reservation succeeds can the home queue the payout. The
   home's federation reservation becomes `COMMITTED` in the same SQLite
   transaction that burns the local nullifier, burns the offer and moves the
   deposit to `QUEUED`.
6. The home asks every peer to commit. Peer commit acknowledgements are stored
   durably; the worker retries missing acknowledgements after API/worker
   restarts. A commit failure never rolls back an already queued exit and never
   triggers an abort.

Endpoints for this flow are:

- `POST /v1/federation/nullifier/reserve`
- `POST /v1/federation/nullifier/commit`
- `POST /v1/federation/nullifier/abort`

Reservations **do not automatically expire**. Automatic expiry could reopen a
double-spend window after a crash. The trade-off is explicit: a crash before
the home queue can strand a reservation and require operator recovery. Do not
delete one unless the operator has established that no home coordinator queued
that authorization.

N-of-N also means new exits pause when any required coordinator is unavailable.
That is a deliberate consistency-over-availability choice, not a claim of
highly available threshold consensus.

## Configuration and authentication boundary

```bash
export SWIRL_FEDERATION_ID=coordinator-a
export SWIRL_FEDERATION_PEERS=https://coordinator-b.example
export SWIRL_FEDERATION_TOKEN='use-a-long-random-shared-secret'
```

Send the token in `X-SWIRL-Federation-Token`. Configure reciprocal peers and
the same token on each coordinator. Production peers must use HTTPS. Plaintext
HTTP requires `SWIRL_FEDERATION_ALLOW_HTTP=true` and is intended only for the
isolated Compose regtest.

The shared bearer token authenticates RPC connections; it is **not** a
coordinator signature, a public consensus identity, or protection against a
peer that already knows the token. Coordinator ids must be unique and each home
runs exactly one API process plus one worker. Signed snapshots, per-coordinator
keys and a key-registry policy remain future work.

Snapshots are capped at 2 MB and 10,000 commitments per coordinator. This
reference implementation is not an unbounded production federation protocol.

## Settlement boundary

- A payout is authorized and executed only by the coordinator that holds the
  named local deposit. A remote commitment returns `unknown_commitment`.
- Federation nullifier reservations are replicated, but used offers, payout
  journals and held-entry preimages remain in the home database.
- There is no remote payment delegation, liquidity compensation, atomic
  cross-coordinator payout, or arbitrary k/n exit authorization.
- Sharing commitments expands the reconstructed Merkle set; it does not hide
  deposits from peers or prevent operator collusion and metadata correlation.
  Direct mode still reveals the deposit-to-exit link to the home coordinator,
  as documented in [POOL.md](POOL.md).

Remote payment delegation requires a separate protocol for liquidity
compensation, partial failure, crash recovery and atomicity. It must not be
inferred from the presence of a shared Merkle root or distributed nullifier.

## Real-node validation

`bash scripts/regtest_federation.sh` runs two coordinators, two hold-enabled
CLN nodes and separate SQLite databases. It opens independent payer channels,
pays one held deposit at each coordinator, waits for both APIs to expose the
same federation root with cross-peer inclusion proofs, and authorizes one
BOLT12 exit against that root at each home coordinator. It then checks that
both databases contain durable `COMMITTED` nullifier records.

The v0.7.0 validation run completed with both new deposits `SETTLED`, both held
entries `paid`, and every recorded nullifier `COMMITTED` on both coordinators,
using CLN v25.09 and hold v0.3.3 on regtest. No real funds were used. The
Compose token and HTTP opt-in are regtest-only settings.
