"""Asset-backend contract shared by BTC and future RGB Lightning pools.

A coordinator serves exactly one asset. Separate assets MUST use separate
coordinator databases, Merkle trees and nullifier sets; amounts and anonymity
sets are not interchangeable across assets.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class AssetSpec:
    """Public, explicit capabilities of one payment backend."""

    backend: str
    asset_id: str
    network: str
    amount_unit: str
    enabled: bool
    settlement: str
    detail: str

    def public(self) -> dict:
        return asdict(self)


class AssetBackend(Protocol):
    """Structural interface consumed by the protocol state machine."""

    spec: AssetSpec

    def create_invoice(
        self, amount_sats: int, label: str, description: str, expiry: int
    ) -> str: ...

    def invoice_status(self, label: str) -> tuple[bool, int | None]: ...

    def invoice_paid(self, label: str) -> bool: ...

    def pay_offer(self, offer: str, amount_sats: int) -> str: ...

    def node_uri(self) -> str: ...
