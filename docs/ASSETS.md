# Asset backends: BTC now, RGB behind an explicit boundary

The protocol state machine consumes the `AssetBackend` interface in
`app/assets.py`. The interface covers standard and held-invoice creation,
held-entry status/claim/cancellation, real payment-time lookup, BOLT12-style
payout, block height and node identity. Backend capabilities are published by
`GET /v1/params` under `asset_backend`.

## BTC

`SWIRL_ASSET_BACKEND=btc` is the default, with sat-denominated fixed lanes and
BOLT12 payouts. `SWIRL_HELD_HTLC=true` enables held BOLT11 entries in both CLN
and the development mock. CLN requires the pinned hold plugin and a safe
invoice-expiry configuration. Without `SWIRL_CLN_RPC`, the backend is the
in-memory development mock and must not be used for real funds.

## RGB

`SWIRL_ASSET_BACKEND=rgb` selects `RGBBackend`, which is intentionally
**disabled**. It advertises `enabled: false`, rejects deposits with HTTP 503
and every direct fund-moving method raises `BackendError`. Setting an RGB
asset ID does not enable it.

This is an architecture boundary, not RGB payment support. RGB uses
client-side validation and does not provide a global EVM-like contract that
forces this coordinator to settle or refund. Merkle roots, nullifiers and any
future Groth16 verification remain coordinator/client protocol logic.

A real RGB backend still requires all of the following before `enabled` may
become true:

1. Select and version-pin one RGB protocol/node route and one RGB-compatible
   Lightning node implementation.
2. Configure the asset contract ID and its exact base-unit precision.
3. Implement asset-aware invoice creation, payment-status lookup and payout;
   derive real paid timestamps and durable payment proof from the node.
4. Define fixed denominations in that asset's base unit. Never reuse the BTC
   sat lattice for an RGB asset.
5. Test expiry, timeout/refund behavior, crash recovery and ambiguous payment
   outcomes on regtest. Never blindly retry a payment.
6. Complete an integration/security review and update the public capability
   document in the same change.

## Isolation rule

Run one coordinator process pair and one database per asset. BTC and RGB pools
must never share a Merkle tree, root history, nullifier set or denomination
configuration. Combining them would mix incompatible units and corrupt the
meaning of the anonymity set.
