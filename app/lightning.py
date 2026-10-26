"""Lightning backends. The coordinator talks to its own CLN node only;
user funds sit in HTLCs on that node's channels, never in a custodial
database balance.

CLNBackend speaks CLN's JSON-RPC over the unix socket directly (stdlib).
MockBackend is for tests and local development.
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

    def __init__(self, rpc_path: str = "/root/.lightning/regtest/lightning-rpc"):
        self.rpc_path = rpc_path
        self._id = 0

    def _call(self, method: str, **params):
        self._id += 1
        req = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        ).encode()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(60)
            s.connect(self.rpc_path)
            s.sendall(req)
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                try:
                    resp = json.loads(buf.decode())
                    break
                except json.JSONDecodeError:
                    continue
        if "error" in resp:
            raise BackendError(f"CLN {method}: {resp['error']}")
        return resp["result"]

    def create_invoice(self, amount_sats: int, label: str, description: str, expiry: int) -> str:
        res = self._call(
            "invoice", amount_msat=amount_sats * 1000,
            label=label, description=description, expiry=expiry,
        )
        return res["bolt11"]

    def invoice_paid(self, label: str) -> bool:
        res = self._call("listinvoices", label=label)
        inv = res.get("invoices", [])
        return bool(inv) and inv[0].get("status") == "paid"

    def pay_offer(self, offer: str, amount_sats: int) -> str:
        """BOLT12 blinded-path payout: fetchinvoice → pay. Returns preimage."""
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
        self._paid: set[str] = set()
        self.payouts: list[dict] = []

    def create_invoice(self, amount_sats: int, label: str, description: str, expiry: int) -> str:
        return f"mockinv1{label}"

    def pay_invoice(self, label: str):
        self._paid.add(label)

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
