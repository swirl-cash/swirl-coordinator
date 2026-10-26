# Refund paths — custody and liveness boundaries

## Implemented in v0.5.0: held-HTLC entries

BTC deposits can use held BOLT11 invoices (hodl-style entries) on today's
Lightning. No soft fork or new cryptographic assumption is required.

1. The coordinator creates an invoice with a private entry preimage and
   injects it into the CLN hold plugin **before returning it to the payer**.
2. An incoming payment remains `accepted`: the HTLC is unresolved and the
   coordinator has not claimed the deposit.
3. The receipt owner signs the root, nullifier, exit offer, amount and fee.
   The coordinator enforces the fixed denomination, minimum hold and jitter.
4. At settlement the worker checks both the wall-clock and CLTV safety
   margins, pays the exit, writes the exit preimage to its fsync'd journal,
   and only then claims the entry HTLC.
5. If an entry is cancelled before settlement, the deposit becomes
   `REFUNDING` and then `REFUNDED`; no exit is started. An unpaid expired
   invoice becomes `EXPIRED`. Cancellation before payout authorization does
   not burn the nullifier.

Enable the CLN path with `SWIRL_HELD_HTLC=true` and the pinned hold plugin in
`Dockerfile.cln-hold`. The invoice expiry must be greater than
`min_hold + jitter_max + settlement_safety_seconds`; invalid configurations
fail at startup. The reference Compose file uses shortened regtest values.

## What happens when the coordinator disappears

While an entry remains unresolved, its native Lightning CLTV timeout is the
refund path. A live coordinator/plugin can actively cancel it; if the whole
node disappears, the depositor waits for the HTLC timeout rather than relying
on an active refund API. This removes the earlier state in which a normal
deposit had already settled and a vanished coordinator could keep it
indefinitely.

This is a liveness and timeout guarantee, not instant recovery. Channel
liquidity remains locked during the hold, and the exact refund completion
depends on the HTLC and channel resolution.

## The remaining trust boundary

The entry and exit are **not one atomic swap**. The coordinator pays the exit
before claiming the entry, creating a bounded operator counterparty window.
If the exit succeeds but the entry is cancelled, the pool can lose funds; the
state becomes `ENTRY_REVIEW`, never a fake `SETTLED`. If a payment RPC outcome
is uncertain, the state becomes `PAYOUT_REVIEW` and the worker never blindly
pays again.

The coordinator can still refuse service, delay settlement, observe its own
logs, or go offline. Users should run their own coordinator or choose one they
trust for availability and metadata behavior. The software is trust-minimized,
not trustless.

## Crash and review states

- `SETTLING`: exit attempted; journal recovery prevents a second payment when
  a successful payment proof exists.
- `EXIT_PAID`: exit proof is durable; entry claim remains to be completed.
- `PAYOUT_REVIEW`: exit result is ambiguous and requires node/payment
  reconciliation before any retry.
- `REFUNDING` / `REFUNDED`: entry cancellation is in progress / confirmed.
- `ENTRY_REVIEW`: exit was paid but the entry could not be claimed; operator
  intervention is required.

The entry preimage is stored in the coordinator database until entry
settlement, and is never returned by the public status API. Protect the
database, settlement journal and their backups as operational secrets. The
user's receipt secret key is still never transmitted or stored.

## Validation status

Unit tests cover full held settlement, cancellation, CLTV/wall-clock safety,
nullifier behavior, ambiguous exits, claim failure and journal recovery.
Real-uvicorn HTTP smoke tests exercise the same state machine with the mock
backend. **A real CLN + hold-plugin regtest run is still required for this
release; mock and RPC-shape tests are not a substitute for node validation.**

## Alternative for large lanes: swap-in with a timelocked refund leg

For the 0.1 / 1 BTC lanes, an on-chain swap-in HTLC can have two spending
paths:

- **cooperative**: coordinator claims with the payment preimage
- **refund**: depositor claims after a locktime (for example 24 h)

This costs an on-chain transaction per deposit, so it is a possible large-lane
option rather than the default small-payment path. It is not implemented here.

## Long-term

- **PTLC + adaptor signatures**: cryptographically couple entry and exit so
  claiming one reveals the secret needed by the other.
- **Federated exits**: k/n coordinator authorization reduces single-operator
  control over settlement.
- **BitVM-class enforcement**: challengeable Bitcoin enforcement is a research
  track, not a current deployment claim.
