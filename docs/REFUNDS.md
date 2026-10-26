# Refund paths — the honest state of custody

## The gap, stated plainly

Today (v0.2.0) a deposit is a normal BOLT11 invoice paid to the coordinator's
node. Once that HTLC settles, the money is in the coordinator's channels.
If the coordinator then refuses to settle your exit — maliciously, or because
it simply vanished — **the protocol cannot currently force a refund**. Signed
payout authorizations stop the coordinator from changing the *terms* of your
exit; they cannot stop it from doing nothing. Everything else in the codebase
is engineered, but this one property is a trust assumption, and we do not
claim otherwise.

What v0.2.0 does give you today:

- your receipt key is never on the coordinator — it cannot invent an exit
- fee and destination are signature-bound — it cannot alter an authorized exit
- every payment is journaled before the state flip — an honest operator can
  always reconcile and finish settlement after a crash
- the code is public — you can run the coordinator yourself and remove the
  operator question entirely

## Near-term design: held-HTLC deposits (hodl-style entry)

The strongest change available on today's Lightning, no soft forks, no new
crypto:

1. The deposit invoice is **not claimed on arrival**. The HTLC sits in the
   channel, unresolved, with its normal CLTV timeout.
2. If the coordinator disappears or refuses service, the HTLC **times out and
   the depositor's wallet refunds automatically** — this is native Lightning
   behavior, not a new mechanism we have to invent.
3. The coordinator claims the entry HTLC **only when it executes the exit
   payout**. Before that moment it never takes ownership of the funds.

Consequences we accept deliberately:

- channel liquidity is locked for the whole hold (60 min + jitter), which
  caps throughput and raises capital cost for the operator
- the coordinator pays the exit before claiming the entry, so it needs its
  own liquidity buffer and takes a small counterparty window in the other
  direction — bounded by claiming entry and exit in the same worker step
- requires hodl-invoice support on the node (CLN plugin / LND native) and
  careful timeout margins so entry HTLCs never expire mid-settlement

This converts "the coordinator can keep your money indefinitely" into "the
coordinator can waste your time, but not your funds" — which is the correct
threat model for a mixing pool.

## Alternative for large lanes: swap-in with a timelocked refund leg

For the 0.1 / 1 BTC lanes, an on-chain swap-in HTLC with two spending paths:

- **cooperative**: coordinator claims with the payment preimage
- **refund**: depositor claims after a locktime (e.g. 24 h)

Costs an on-chain transaction per deposit, so it is wrong for small lanes and
right for large ones. Shares the anonymous-set design with submarine exits.

## Long-term

- **PTLC + adaptor signatures**: entry and exit become cryptographically
  coupled — claiming the entry reveals the secret that completes the exit.
  This is the Lightning-native endgame for atomic in/out.
- **Federated exits**: k/n coordinator multisig on the exit side, so a single
  operator cannot even refuse alone.
- **BitVM-class enforcement**: the exit condition itself becomes
  challengeable on Bitcoin. Research track, not a promise.

Until at least the held-HTLC entry ships and passes regtest, the honest
one-liner stays on the README: **trust-minimized, not trustless.**
