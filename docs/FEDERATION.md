# Federated anonymity-set rounds

Implemented in v0.6.0. Federation currently shares anonymity sets; it does not
make exits atomic across coordinators.

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
5. The combined root enters the same rolling 30-root authorization history as
   a local root. Inclusion proofs can cover local and remote commitments.

If a peer is unavailable or returns an invalid snapshot, the last valid stored
snapshot remains in use. Until at least two valid snapshots exist, the API
falls back to the local set. Different nodes can temporarily expose different
rounds while new deposits propagate; clients should use the root returned by
their home coordinator and fetch its inclusion proof.

## Configuration

```bash
export SWIRL_FEDERATION_ID=coordinator-a
export SWIRL_FEDERATION_PEERS=https://coordinator-b.example
export SWIRL_FEDERATION_TOKEN='use-a-long-random-shared-secret'
```

Send the token in `X-SWIRL-Federation-Token`. Configure the reciprocal peer and
the same token on the other coordinator. Production peers must use HTTPS.
Plaintext HTTP requires `SWIRL_FEDERATION_ALLOW_HTTP=true` and is intended only
for the isolated Compose regtest. The token authenticates the peer connection;
it is not a coordinator signature or a public consensus identity. Deployments
that need independently verifiable coordinator identities still need signed
snapshots and a key-registry policy.

Snapshots are capped at 2 MB and 10,000 commitments per coordinator. This
reference implementation is therefore not an unbounded production federation
protocol.

## Settlement boundary

- A payout is authorized and executed only by the coordinator that holds the
  named local deposit. A remote commitment returns `unknown_commitment`.
- Nullifiers, used offers, payout journals and held-entry preimages remain in
  the home coordinator's database.
- There is no distributed nullifier set, k/n exit authorization, atomic
  cross-coordinator payout, or federation-wide crash recovery in this release.
- Sharing commitments expands the reconstructed Merkle set; it does not hide
  deposits from peer coordinators or prevent operator collusion and metadata
  correlation. Direct mode still reveals the deposit-to-exit link to the home
  coordinator, as documented in [POOL.md](POOL.md).

Cross-coordinator exits require a separate protocol for distributed nullifier
finality, authorization, partial failure and recovery. They must not be inferred
from the presence of a shared Merkle root.

## Real-node validation

`bash scripts/regtest_federation.sh` runs two coordinators, two hold-enabled
CLN nodes and separate SQLite databases. It opens independent payer channels,
pays one held deposit at each coordinator, waits for both APIs to expose the
same federation root with cross-peer inclusion proofs, and authorizes one
BOLT12 exit against that root at each home coordinator.

The v0.6.0 validation run completed with both deposits `SETTLED` and both held
entries `paid`, using CLN v25.09 and hold v0.3.3 on regtest. No real funds were
used. The Compose token and HTTP opt-in are regtest-only settings.
