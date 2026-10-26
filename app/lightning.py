"""Lightning backends. The coordinator talks to its own CLN node only;
user funds sit in HTLCs on that node's channels, never in a custodial
database balance.

CLNBackend speaks CLN's JSON-RPC over the unix socket directly (stdlib):
  * incremental JSON framing — handles fragmented and multi-object reads
  * skips async notifications, matches responses by request id
  * separate connect/read timeouts
  * reconnects and retries ONLY idempotent reads — payments are never
    retried blindly, because a lost response is not a failed payment
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import secrets
import time
from datetime import datetime, timezone

from .assets import AssetSpec, HeldInvoice, HeldInvoiceStatus


class BackendError(RuntimeError):
    pass


class PaymentAmbiguous(BackendError):
    """The node may have paid; automatic retry could double-pay."""


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off", ""):
        return False
    raise ValueError(f"{name} must be true or false")


def _timestamp(value) -> int | None:
    """Parse CLN integers and hold-plugin naive-UTC ISO timestamps."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


class CLNBackend:
    """Core Lightning over its JSON-RPC unix socket."""

    CONNECT_TIMEOUT = 10
    READ_TIMEOUT = 75
    READ_METHODS = {
        "listinvoices", "getinfo", "listpays", "listpayments",
        "listholdinvoices",
    }

    def __init__(self, rpc_path: str = "/root/.lightning/regtest/lightning-rpc",
                 held_htlc: bool | None = None):
        self.rpc_path = rpc_path
        self._id = 0
        self.held_invoices_enabled = (
            _env_flag("SWIRL_HELD_HTLC") if held_htlc is None else held_htlc
        )
        self.spec = AssetSpec(
            backend="btc-cln",
            asset_id="btc",
            network=os.environ.get("SWIRL_NETWORK", "regtest"),
            amount_unit="sats",
            enabled=True,
            settlement=(
                "held BOLT11 entries; BOLT12 blinded-path payouts"
                if self.held_invoices_enabled else
                "BOLT11 deposits; BOLT12 blinded-path payouts"
            ),
            detail="Core Lightning JSON-RPC",
        )

    def _roundtrip(self, method: str, params: dict):
        self._id += 1
        req_id = self._id
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        ).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(self.CONNECT_TIMEOUT)
            s.connect(self.rpc_path)
            s.sendall(payload)
            s.settimeout(self.READ_TIMEOUT)
            decoder = json.JSONDecoder()
            buf = ""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    raise BackendError(f"CLN {method}: connection closed mid-response")
                buf += chunk.decode(errors="replace")
                # a read may contain several objects (responses + notifications)
                while True:
                    stripped = buf.lstrip()
                    if not stripped:
                        buf = ""
                        break
                    try:
                        obj, end = decoder.raw_decode(stripped)
                    except json.JSONDecodeError:
                        buf = stripped  # incomplete object — wait for more bytes
                        break
                    buf = stripped[end:]
                    if obj.get("id") != req_id:
                        continue  # async notification or stale response
                    if "error" in obj:
                        raise BackendError(f"CLN {method}: {obj['error']}")
                    return obj["result"]

    def _call(self, method: str, **params):
        attempts = 3 if method in self.READ_METHODS else 1
        last: Exception | None = None
        for i in range(attempts):
            try:
                return self._roundtrip(method, params)
            except (OSError, TimeoutError) as e:
                last = e
                if i + 1 < attempts:
                    time.sleep(0.5 * (i + 1))
        raise BackendError(f"CLN {method}: {last}")

    def create_invoice(self, amount_sats: int, label: str, description: str, expiry: int) -> str:
        res = self._call(
            "invoice", amount_msat=amount_sats * 1000,
            label=label, description=description, expiry=expiry,
        )
        return res["bolt11"]

    def create_held_invoice(self, amount_sats: int, label: str,
                            description: str, expiry: int) -> HeldInvoice:
        """Create privately, inject into the hold plugin, then expose.

        The invoice is never returned to a payer before injection, so the
        ordinary CLN invoice cannot be auto-settled in a creation race.
        """
        if not self.held_invoices_enabled:
            raise BackendError("held HTLC support is disabled")
        preimage = secrets.token_bytes(32)
        payment_hash = hashlib.sha256(preimage).hexdigest()
        created = self._call(
            "invoice", amount_msat=amount_sats * 1000, label=label,
            description=description, expiry=expiry,
            preimage=preimage.hex(),
        )
        self._call("injectholdinvoice", invoice=created["bolt11"])
        return HeldInvoice(created["bolt11"], payment_hash, preimage.hex())

    def held_invoice_status(self, payment_hash: str) -> HeldInvoiceStatus:
        res = self._call("listholdinvoices", payment_hash=payment_hash)
        invoices = res.get("holdinvoices", [])
        if not invoices:
            raise BackendError("hold invoice is missing from the plugin")
        inv = invoices[0]
        state = str(inv.get("state", "")).lower()
        if state not in ("unpaid", "accepted", "paid", "cancelled"):
            raise BackendError(f"unknown hold invoice state: {state!r}")
        htlcs = inv.get("htlcs", [])
        expiries = [h["cltv_expiry"] for h in htlcs
                    if h.get("cltv_expiry") is not None]
        accepted_at = None
        if state == "accepted":
            times = [_timestamp(h.get("created_at")) for h in htlcs]
            accepted_at = max((t for t in times if t is not None), default=None)
        elif state == "paid":
            accepted_at = _timestamp(inv.get("settled_at"))
        if accepted_at is None and state in ("accepted", "paid"):
            accepted_at = _timestamp(inv.get("created_at"))
        return HeldInvoiceStatus(
            state, accepted_at, min(expiries) if expiries else None,
        )

    def settle_held_invoice(self, preimage: str) -> None:
        self._call("settleholdinvoice", preimage=preimage)

    def cancel_held_invoice(self, payment_hash: str) -> None:
        self._call("cancelholdinvoice", payment_hash=payment_hash)

    def invoice_status(self, label: str) -> tuple[bool, int | None]:
        """(paid, paid_at). paid_at is the node's real payment timestamp,
        not the moment our poller happened to notice."""
        res = self._call("listinvoices", label=label)
        inv = res.get("invoices", [])
        if inv and inv[0].get("status") == "paid":
            return True, inv[0].get("paid_at")
        return False, None

    def invoice_paid(self, label: str) -> bool:
        return self.invoice_status(label)[0]

    def pay_offer(self, offer: str, amount_sats: int) -> str:
        """BOLT12 blinded-path payout: fetchinvoice → pay. Returns preimage.

        Any failed pay RPC is treated as ambiguous. The coordinator must
        reconcile listpays before it can safely retry the exit.
        """
        fetched = self._call("fetchinvoice", offer=offer, amount_msat=amount_sats * 1000)
        try:
            res = self._call("pay", bolt11=fetched["invoice"])
        except BackendError as e:
            raise PaymentAmbiguous(
                "exit payment outcome is unknown; reconcile listpays before retry"
            ) from e
        return res.get("payment_preimage", "")

    def block_height(self) -> int:
        return int(self._call("getinfo")["blockheight"])

    def node_uri(self) -> str:
        info = self._call("getinfo")
        return info.get("id", "")


class MockBackend:
    """Deterministic standard/held-invoice backend for tests."""

    def __init__(self, held_htlc: bool = False):
        self._paid: dict[str, int] = {}
        self._held: dict[str, dict] = {}
        self._held_labels: dict[str, str] = {}
        self.payouts: list[dict] = []
        self.settled_entries: list[str] = []
        self.cancelled_entries: list[str] = []
        self.clock = time.time
        self.height = 1_000
        self.held_invoices_enabled = held_htlc
        self.spec = AssetSpec(
            backend="btc-mock",
            asset_id="btc",
            network="mock",
            amount_unit="sats",
            enabled=True,
            settlement=(
                "mock held invoices and payouts; development only"
                if held_htlc else "mock invoices and payouts; development only"
            ),
            detail="in-memory BTC backend; never use with real funds",
        )

    def create_invoice(self, amount_sats: int, label: str, description: str, expiry: int) -> str:
        return f"mockinv1{label}"

    def create_held_invoice(self, amount_sats: int, label: str,
                            description: str, expiry: int) -> HeldInvoice:
        if not self.held_invoices_enabled:
            raise BackendError("held HTLC support is disabled")
        preimage = secrets.token_bytes(32).hex()
        payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        self._held[payment_hash] = {
            "state": "unpaid", "preimage": preimage, "accepted_at": None,
            "expiry": self.height + 144,
        }
        self._held_labels[label] = payment_hash
        return HeldInvoice(f"mockheld1{label}", payment_hash, preimage)

    def pay_invoice(self, label: str):
        if label in self._held_labels:
            entry = self._held[self._held_labels[label]]
            if entry["state"] == "unpaid":
                entry["state"] = "accepted"
                entry["accepted_at"] = int(self.clock())
            return
        self._paid[label] = int(self.clock())

    def held_invoice_status(self, payment_hash: str) -> HeldInvoiceStatus:
        entry = self._held[payment_hash]
        return HeldInvoiceStatus(
            entry["state"], entry["accepted_at"], entry["expiry"],
        )

    def settle_held_invoice(self, preimage: str) -> None:
        payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        entry = self._held[payment_hash]
        if entry["state"] == "cancelled":
            raise BackendError("cannot settle a cancelled hold invoice")
        entry["state"] = "paid"
        self.settled_entries.append(payment_hash)

    def cancel_held_invoice(self, payment_hash: str) -> None:
        entry = self._held[payment_hash]
        if entry["state"] == "paid":
            raise BackendError("cannot cancel a paid hold invoice")
        entry["state"] = "cancelled"
        self.cancelled_entries.append(payment_hash)

    def invoice_status(self, label: str) -> tuple[bool, int | None]:
        ts = self._paid.get(label)
        return (ts is not None), ts

    def invoice_paid(self, label: str) -> bool:
        return label in self._paid

    def pay_offer(self, offer: str, amount_sats: int) -> str:
        preimage = secrets.token_bytes(32).hex()
        self.payouts.append({"offer": offer, "amount_sats": amount_sats,
                             "preimage": preimage, "at": int(time.time())})
        return preimage

    def block_height(self) -> int:
        return self.height

    def node_uri(self) -> str:
        return hashlib.sha256(b"mock-node").hexdigest()[:66]


def from_env():
    """Select exactly one asset backend; unknown selections fail at startup."""
    selected = os.environ.get("SWIRL_ASSET_BACKEND", "btc").strip().lower()
    if selected == "rgb":
        from .rgb import RGBBackend
        return RGBBackend()
    if selected != "btc":
        raise ValueError(
            f"unsupported SWIRL_ASSET_BACKEND={selected!r}; expected 'btc' or 'rgb'"
        )
    rpc = os.environ.get("SWIRL_CLN_RPC", "")
    return CLNBackend(rpc) if rpc else MockBackend(_env_flag("SWIRL_HELD_HTLC"))
