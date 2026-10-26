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


class BackendError(RuntimeError):
    pass


class CLNBackend:
    """Core Lightning over its JSON-RPC unix socket."""

    CONNECT_TIMEOUT = 10
    READ_TIMEOUT = 75
    READ_METHODS = {"listinvoices", "getinfo", "listpays", "listpayments"}

    def __init__(self, rpc_path: str = "/root/.lightning/regtest/lightning-rpc"):
        self.rpc_path = rpc_path
        self._id = 0

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

        NOT auto-retried: if the connection dies mid-payment the worker must
        reconcile via listpays instead of paying twice.
        """
        fetched = self._call("fetchinvoice", offer=offer, amount_msat=amount_sats * 1000)
        res = self._call("pay", bolt11=fetched["invoice"])
        return res.get("payment_preimage", "")

    def node_uri(self) -> str:
        info = self._call("getinfo")
        return info.get("id", "")


class MockBackend:
    """Deterministic in-memory backend for tests. Invoices start unpaid;
    tests pay them explicitly with pay_invoice()."""

    def __init__(self):
        self._paid: dict[str, int] = {}
        self.payouts: list[dict] = []
        self.clock = time.time

    def create_invoice(self, amount_sats: int, label: str, description: str, expiry: int) -> str:
        return f"mockinv1{label}"

    def pay_invoice(self, label: str):
        self._paid[label] = int(self.clock())

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

    def node_uri(self) -> str:
        return hashlib.sha256(b"mock-node").hexdigest()[:66]


def from_env() -> "CLNBackend | MockBackend":
    rpc = os.environ.get("SWIRL_CLN_RPC", "")
    return CLNBackend(rpc) if rpc else MockBackend()
