# What we took from Tornado — and what is still ahead

We studied `tornado-core` (Tornado.sol, MerkleTreeWithHistory.sol,
circuits/withdraw.circom), `tornado-relayer`, `tornado-anonymity-mining`
and `torn-token`. This document records exactly which mechanisms v0.3.0
adopts, and which ones remain open work. No cargo-culting: each item says
WHY it exists.

## Adopted in v0.3.0

| Tornado mechanism | Where it lives here | Why it matters |
|---|---|---|
| Permanent append-only Merkle tree; deposits are never removed on withdrawal | `app/merkle.py`, `leaves` table | Our earlier "live commitments only" tree SHRANK the anonymity set with every settlement. Tornado's set only grows. That was a real design bug, now fixed. |
| Rolling root history (30 roots) and `isKnownRoot` at withdrawal | `roots` table, `unknown_root` error | A withdrawal referencing a brand-new root would leak "this user deposited a second ago". Any of the last 30 roots is accepted. |
| Nullifier set for double-spends, independent of the deposit state machine | `nullifiers` table, `receipts.nullifier_hash_for` | Even a state-machine bug cannot let a note be spent twice. `already_spent` is final. |
| Withdrawal public inputs = (root, nullifierHash, recipient, fee) | `receipts.auth_digest` | Exactly the tuple tornado's circuit binds. Our direct mode signs it with BIP340; the Groth16 mode will prove the same tuple. |
| Denomination-enforced pools, fee taken inside the withdrawal | `app/lattice.py` | Unique amounts are fingerprints; fees must not reintroduce them. |
| Relayer fee is part of the signed/proven statement | `fee_bps` in the digest | Neither the coordinator nor a future relayer can reprice an authorized exit. |

## The honest difference: where the ZK goes

Tornado's verifier is an on-chain contract: `verifyProof` is enforced by
EVM consensus, so the operator cannot lie about membership. Our coordinator
verifies off-chain. Two different trust statements:

- **Direct mode (today)**: the withdrawal names its commitment and carries a
  Schnorr signature. The coordinator learns the deposit↔exit link (it must —
  it checks the key), but cannot alter terms. Nullifier + root history are
  already enforced here.
- **Groth16 mode (next milestone)**: the withdrawal presents ONLY
  (root, nullifierHash, offer, fee) plus a proof. The coordinator learns
  nothing about which leaf is being spent — the same privacy tornado has
  against its own relayers. This requires the full circuit toolchain
  (circom circuits, powers-of-tau + circuit-specific setup, prover in the
  user CLI, verifier in the coordinator). It ships as one tested module or
  not at all — we will not expose a "zk" flag that verifies nothing.

## Deliberately NOT copied

- **EVM specifics**: Pedersen/MiMC are circuit-friendly hashes chosen for
  cheap constraints. Until the circuit lands, SHA-256 is the better tool;
  the tree's shape (20 levels, root history, paths) is already final so the
  hash swap touches one module.
- **TORN tokenomics**: our SWIRL allocation and governance live in the site
  docs; anonymity mining (shielded AP accounts) is a separate module, not
  something to bolt onto the settlement path.
- **Refund by contract**: EVM pools can't be rugged by the contract itself;
  Lightning HTLCs can be held by the operator. Our answer is the held-HTLC
  entry in [REFUNDS.md](REFUNDS.md), not pretending the models are identical.
