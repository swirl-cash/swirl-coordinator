"""RGB Lightning backend boundary.

This is deliberately a disabled adapter, not a pretend RGB implementation.
Invoice creation, payment-status reads and payouts all fail closed until a
version-pinned RGB Lightning node integration has been implemented and tested.
"""

from __future__ import annotations

import os

from .assets import AssetSpec
from .lightning import BackendError


class RGBBackend:
    """Fail-closed placeholder for a future RGB-compatible Lightning node."""

    def __init__(self) -> None:
        asset_id = os.environ.get("SWIRL_RGB_ASSET_ID", "").strip()
        self.spec = AssetSpec(
            backend="rgb",
            asset_id=asset_id or "unconfigured",
            network=os.environ.get("SWIRL_NETWORK", "regtest"),
            amount_unit="asset_base_unit",
            enabled=False,
            settlement="unavailable: RGB Lightning node integration required",
            detail=(
                "adapter boundary only; no RGB invoices or payouts are accepted"
            ),
        )

    def _unavailable(self) -> None:
        raise BackendError(
            "RGB backend is not connected; refusing all fund-moving operations"
        )

    def create_invoice(
        self, amount_sats: int, label: str, description: str, expiry: int
    ) -> str:
        self._unavailable()
        raise AssertionError("unreachable")

    def invoice_status(self, label: str) -> tuple[bool, int | None]:
        self._unavailable()
        raise AssertionError("unreachable")

    def invoice_paid(self, label: str) -> bool:
        self._unavailable()
        raise AssertionError("unreachable")

    def pay_offer(self, offer: str, amount_sats: int) -> str:
        self._unavailable()
        raise AssertionError("unreachable")

    def node_uri(self) -> str:
        self._unavailable()
        raise AssertionError("unreachable")
