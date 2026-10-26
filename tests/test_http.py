"""HTTP smoke tests through a real uvicorn socket with the in-process mock.

Worker rounds are invoked explicitly because the mock is intentionally
in-memory; a separate worker process could not share its payment state.
These tests do not claim real-CLN coverage.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

import uvicorn

from app import receipts, schnorr
from app.main import build_app


class UvicornSmoke(unittest.TestCase):
    held = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {
            "SWIRL_ASSET_BACKEND": "btc",
            "SWIRL_CLN_RPC": "",
            "SWIRL_DB": os.path.join(self.tmp.name, "smoke.db"),
            "SWIRL_HELD_HTLC": "true" if self.held else "false",
            "SWIRL_MIN_HOLD": "0",
            "SWIRL_JITTER_MAX": "0",
            "SWIRL_INVOICE_EXPIRY": "60",
            "SWIRL_SETTLEMENT_SAFETY_SECONDS": "1",
            "SWIRL_SETTLEMENT_SAFETY_BLOCKS": "2",
        }
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.app = build_app()
        self.coord = self.app.state.coord
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port,
            log_level="error", access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if self.request("GET", "/v1/health")["ok"]:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            self.fail("uvicorn did not start")

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.coord.store.close()
        self.env.stop()
        self.tmp.cleanup()

    def request(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read())

    def deposit(self):
        secret = secrets.token_bytes(32)
        amount = 1_000_000
        pubkey = schnorr.pubkey(secret)
        commitment = receipts.make_commitment(pubkey, amount)
        result = self.request("POST", "/v1/mix/deposit", {
            "amount_sats": amount, "pubkey": pubkey.hex(),
        })
        self.assertEqual(result["commitment"], commitment)
        self.assertNotIn("entry_preimage", result)
        return secret, amount, commitment, result

    def accept(self, commitment):
        self.coord.backend.pay_invoice(f"swirl-{commitment[:40]}")
        self.coord.poll_invoices()
        status = self.request("GET", f"/v1/mix/deposit/{commitment}")
        self.assertEqual(status["state"], "HELD")
        return status

    def authorize(self, secret, amount, commitment, offer):
        root = self.request("GET", "/v1/mix/set")["merkle_root"]
        pubkey, nullifier, signature = receipts.sign_authorization(
            receipts.encode_receipt(secret, amount), commitment, root, offer, 50,
        )
        queued = self.request("POST", "/v1/mix/payout", {
            "commitment": commitment, "root": root,
            "nullifier_hash": nullifier, "pubkey": pubkey,
            "offer": offer, "fee_bps": 50, "signature": signature,
        })
        self.assertEqual(queued["state"], "QUEUED")
        self.coord.execute_due_payouts()
        status = self.request("GET", f"/v1/mix/deposit/{commitment}")
        self.assertEqual(status["state"], "SETTLED")
        self.assertNotIn("entry_preimage", status)
        return status

    def test_http_deposit_hold_and_payout(self):
        secret, amount, commitment, created = self.deposit()
        expected_mode = "held_htlc" if self.held else "standard"
        self.assertEqual(created["entry_mode"], expected_mode)
        self.accept(commitment)
        settled = self.authorize(secret, amount, commitment, "lno1-http-smoke")
        self.assertEqual(settled["entry_mode"], expected_mode)
        self.assertEqual(len(self.coord.backend.payouts), 1)
        if self.held:
            self.assertEqual(self.coord.backend.settled_entries,
                             [created["payment_hash"]])


class HeldUvicornSmoke(UvicornSmoke):
    held = True

    def test_http_cancelled_entry_becomes_refunded(self):
        _, _, commitment, created = self.deposit()
        self.accept(commitment)
        self.coord.backend.cancel_held_invoice(created["payment_hash"])
        self.coord.poll_invoices()
        status = self.request("GET", f"/v1/mix/deposit/{commitment}")
        self.assertEqual(status["state"], "REFUNDED")
        self.assertEqual(status["entry_state"], "cancelled")
        self.assertEqual(self.coord.backend.payouts, [])


if __name__ == "__main__":
    unittest.main()
