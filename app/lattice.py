"""Denomination lattice — the only amounts the pool accepts.

A unique amount is a fingerprint. Deposits and payouts are cut from fixed
quanta only; anything else is rejected before it reaches a channel.
"""

# sats — 0.001 / 0.01 / 0.1 / 1 BTC
LATTICE = (100_000, 1_000_000, 10_000_000, 100_000_000)

# launch fee: 50 bps on every lane
FEE_BPS = 50


class LatticeViolation(ValueError):
    """Amount is not exactly one lattice quantum."""


def check_quantum(amount_sats: int) -> int:
    """Return the amount if it is a single quantum, else raise."""
    if not isinstance(amount_sats, int) or isinstance(amount_sats, bool):
        raise LatticeViolation("amount must be an integer number of sats")
    if amount_sats not in LATTICE:
        raise LatticeViolation(
            "amount_not_on_lattice: enter as multiple quanta, one deposit each"
        )
    return amount_sats


def decompose(amount_sats: int) -> list[int]:
    """Greedy cut of a payout into lattice quanta + residual.

    The residual is never paid as-is; it is carried back into the pool.
    """
    if amount_sats < 0:
        raise ValueError("negative amount")
    chunks: list[int] = []
    rest = amount_sats
    for q in sorted(LATTICE, reverse=True):
        while rest >= q:
            chunks.append(q)
            rest -= q
    if rest:
        chunks.append(rest)  # residual — caller must not pay it out directly
    return chunks


def fee_for(amount_sats: int) -> int:
    """Pool fee in sats, deducted inside the pool before payout."""
    return amount_sats * FEE_BPS // 10_000


def payout_amount(amount_sats: int) -> int:
    """What actually leaves the pool for a given quantum."""
    return amount_sats - fee_for(amount_sats)
