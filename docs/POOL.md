# SWIRL core pool model

This document defines the pool mechanisms implemented in v0.7.0 and the trust
boundaries that remain explicit.

## Enforced mechanisms

| Mechanism | Where it lives | Why it matters |
|---|---|---|
| Permanent append-only Merkle tree; deposits are never removed on withdrawal | `app/merkle.py`, `leaves` table | The anonymity set only grows. Rebuilding it from unsettled deposits would shrink the crowd after every withdrawal. |
| Rolling history of 30 recent roots | `roots` table, `unknown_root` error | A withdrawal may reference any recent root instead of revealing a root created immediately after its own deposit. |
| Nullifier set independent of the deposit state machine | `nullifiers` table, `receipts.nullifier_hash_for` | A withdrawal permanently burns its nullifier. `already_spent` is final, even if deposit state handling fails. |
| Authorization binds root, nullifier, destination, amount and fee | `receipts.auth_digest` | Direct mode signs this tuple with BIP340. A future proof mode must bind the same terms. |
| Fixed-denomination pools with the fee deducted inside the pool | `app/lattice.py` | Unique payment amounts are fingerprints; fees must not create a new amount fingerprint. |
| Fee and destination are part of the signed statement | `fee_bps` and the offer hash in the digest | The coordinator cannot reprice or redirect an authorized exit. |

## Authorization and zero-knowledge boundary

The coordinator currently verifies withdrawal authorization off-chain. There
is no blockchain contract or RGB consensus rule that forces it to accept a
proof, settle a payout or issue a refund.

- **Direct mode (today):** the withdrawal names its commitment and carries a
  Schnorr signature. The coordinator learns the deposit-to-exit link, but
  cannot alter the authorized terms. Nullifier and root-history checks are
  enforced by the coordinator.
- **Groth16 mode (future):** the withdrawal should reveal only the root,
  nullifier hash, destination and fee plus a valid proof, without naming the
  commitment. This requires a complete circuit, trusted-setup/proving and
  verification toolchain. No placeholder `zk` switch is exposed.

## Implementation choices and open work

- SHA-256 with domain separation is used for commitments, leaves and internal
  nodes. If a future circuit needs a circuit-friendly hash, that change must
  be versioned and tested end to end.
- SWIRL allocation and governance are documented separately from settlement;
  reward accounting must not weaken receipt or nullifier isolation.
- Held-HTLC entries can remain unresolved until the authorized exit is paid;
  cancellation and native CLTV timeout provide the refund path. Entry and exit
  are not atomic: the operator has a bounded counterparty window, and ambiguous
  payouts or lost entries require explicit review. See [REFUNDS.md](REFUNDS.md).
- BTC and future RGB pools use separate databases, trees, nullifier sets and
  denominations. See [ASSETS.md](ASSETS.md).
- Coordinators combine authenticated local snapshots into deterministic
  federation roots and serve cross-peer inclusion proofs. Before a home queues
  an exit, it and every configured peer durably reserve the nullifier; peer
  commits retry after recovery. Federated exits require the current shared
  root. Payment execution, offers, journals and entry secrets remain at the
  home coordinator. See [FEDERATION.md](FEDERATION.md).
