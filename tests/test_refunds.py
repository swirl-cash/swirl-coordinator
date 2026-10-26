"""Held-HTLC entry, refund and crash-recovery tests."""

import hashlib
import os
import secrets
import sqlite3
import tempfile
import unittest

from app import receipts, schnorr
from app.lightning import (
    BackendError, CLNBackend, MockBackend, PaymentAmbiguous,
)
from app.service import Config, Coordinator, ServiceError
from app.store import Store

Q = 1_000_000
OFFER = "lno1qgsyxj6q9p8qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"


class FakeClock:
    def __init__(self):
        self.t = 1_700_000_000

    def __call__(self):
        return self.t


class HeldCoordinatorCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = MockBackend(held_htlc=True)
        self.backend.clock = self.clock
        self.coord = self.make_coordinator(Store(":memory:"))
        self.sk = secrets.token_bytes(32)
        self.pk_hex = schnorr.pubkey(self.sk).hex()
        self.receipt = receipts.encode_receipt(self.sk, Q)

    def make_coordinator(self, store):
        return Coordinator(
            store, self.backend,
            Config(
                min_hold=60, jitter_max=0, invoice_expiry=1200,
                settlement_safety_seconds=60, settlement_safety_blocks=12,
            ),
            randbelow=lambda n: 0, clock=self.clock,
        )

    def deposit(self):
        res = self.coord.create_deposit(Q, self.pk_hex)
        self.backend.pay_invoice(f"swirl-{res['commitment'][:40]}")
        self.coord.poll_invoices()
        return res

    def authorize(self, commitment):
        root = self.coord.anonymity_set()["merkle_root"]
        pk, nh, sig = receipts.sign_authorization(
            self.receipt, commitment, root, OFFER, 50,
        )
        return self.coord.authorize_payout(
            commitment, root, nh, pk, OFFER, 50, sig,
        )

    def queued_deposit(self):
        res = self.deposit()
        self.authorize(res["commitment"])
        return res


class TestHeldFlow(HeldCoordinatorCase):
    def test_config_requires_a_real_settlement_window(self):
        with self.assertRaisesRegex(ValueError, "invoice_expiry"):
            Coordinator(
                Store(":memory:"), MockBackend(held_htlc=True),
                Config(min_hold=3600, jitter_max=900, invoice_expiry=3600),
            )

    def test_full_held_flow_claims_entry_only_after_exit(self):
        res = self.deposit()
        c = res["commitment"]
        self.assertEqual(res["entry_mode"], "held_htlc")
        self.assertTrue(res["invoice"].startswith("mockheld1"))
        self.assertEqual(self.coord.status(c)["state"], "HELD")
        self.assertEqual(self.backend.settled_entries, [])

        self.authorize(c)
        self.clock.t += 61
        self.assertEqual(self.coord.execute_due_payouts(), [c])
        status = self.coord.status(c)
        self.assertEqual(status["state"], "SETTLED")
        self.assertEqual(status["entry_state"], "paid")
        self.assertIsNotNone(status["entry_settled_at"])
        self.assertNotIn("entry_preimage", status)
        self.assertEqual(len(self.backend.payouts), 1)
        self.assertEqual(self.backend.settled_entries, [res["payment_hash"]])
        self.assertIsNone(self.coord.store.get(c)["entry_preimage"])

    def test_cancelled_entry_refunds_without_an_exit(self):
        res = self.queued_deposit()
        c = res["commitment"]
        self.backend.cancel_held_invoice(res["payment_hash"])
        self.coord.poll_invoices()
        self.assertEqual(self.coord.status(c)["state"], "REFUNDED")
        self.clock.t += 61
        self.assertEqual(self.coord.execute_due_payouts(), [])
        self.assertEqual(self.backend.payouts, [])

    def test_near_cltv_expiry_cancels_instead_of_paying(self):
        res = self.queued_deposit()
        c = res["commitment"]
        expiry = self.coord.status(c)["entry_expiry"]
        self.backend.height = expiry - 12
        self.clock.t += 61
        self.assertEqual(self.coord.execute_due_payouts(), [])
        self.assertEqual(self.coord.status(c)["state"], "REFUNDING")
        self.coord.poll_invoices()
        self.assertEqual(self.coord.status(c)["state"], "REFUNDED")
        self.assertEqual(self.backend.payouts, [])

    def test_cancelled_before_authorization_does_not_burn_nullifier(self):
        res = self.deposit()
        self.backend.cancel_held_invoice(res["payment_hash"])
        with self.assertRaises(ServiceError) as cm:
            self.authorize(res["commitment"])
        self.assertEqual(cm.exception.code, "entry_refunded")
        self.assertEqual(self.coord.status(res["commitment"])["state"], "REFUNDED")
        nh = receipts.nullifier_hash_for(self.sk)
        self.assertIsNone(self.coord.store._q(
            "SELECT 1 FROM nullifiers WHERE nullifier_hash=?", (nh,), "one",
        ))

    def test_entry_claim_failure_resumes_without_repaying_exit(self):
        res = self.queued_deposit()
        c = res["commitment"]
        original = self.backend.settle_held_invoice

        def unavailable(_):
            raise BackendError("node unavailable")
        self.backend.settle_held_invoice = unavailable
        self.clock.t += 61
        self.assertEqual(self.coord.execute_due_payouts(), [])
        self.assertEqual(self.coord.status(c)["state"], "EXIT_PAID")
        self.assertEqual(len(self.backend.payouts), 1)

        self.backend.settle_held_invoice = original
        self.assertEqual(self.coord.execute_due_payouts(), [c])
        self.assertEqual(self.coord.status(c)["state"], "SETTLED")
        self.assertEqual(len(self.backend.payouts), 1)

    def test_exit_paid_but_entry_cancelled_is_not_fake_success(self):
        res = self.queued_deposit()
        c = res["commitment"]

        def cancel_instead(_):
            self.backend.cancel_held_invoice(res["payment_hash"])
        self.backend.settle_held_invoice = cancel_instead
        self.clock.t += 61
        self.assertEqual(self.coord.execute_due_payouts(), [])
        self.assertEqual(self.coord.status(c)["state"], "ENTRY_REVIEW")
        self.assertEqual(self.coord.status(c)["entry_state"], "cancelled")
        self.assertEqual(len(self.backend.payouts), 1)

    def test_ambiguous_exit_never_automatically_retries(self):
        res = self.queued_deposit()
        c = res["commitment"]
        original = self.backend.pay_offer

        def unknown_outcome(*_):
            raise PaymentAmbiguous("connection lost after pay")
        self.backend.pay_offer = unknown_outcome
        self.clock.t += 61
        self.assertEqual(self.coord.execute_due_payouts(), [])
        self.assertEqual(self.coord.status(c)["state"], "PAYOUT_REVIEW")

        self.backend.pay_offer = original
        self.assertEqual(self.coord.execute_due_payouts(), [])
        self.assertEqual(self.coord.status(c)["state"], "PAYOUT_REVIEW")
        self.assertEqual(self.backend.payouts, [])

    def test_journal_recovery_claims_entry_without_second_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "held.db"))
            coord = self.make_coordinator(store)
            res = coord.create_deposit(Q, self.pk_hex)
            c = res["commitment"]
            self.backend.pay_invoice(f"swirl-{c[:40]}")
            coord.poll_invoices()
            root = coord.anonymity_set()["merkle_root"]
            pk, nh, sig = receipts.sign_authorization(
                self.receipt, c, root, OFFER, 50,
            )
            coord.authorize_payout(c, root, nh, pk, OFFER, 50, sig)
            self.assertTrue(store.transition(c, "QUEUED", "SETTLING"))
            coord._journal_append({
                "commitment": c, "preimage": "aa" * 32,
                "amount_sats": Q - 5000, "at": self.clock.t,
            })

            recovered = self.make_coordinator(store)
            recovered.reconcile_settling()
            self.assertEqual(recovered.status(c)["state"], "EXIT_PAID")
            self.assertEqual(recovered.execute_due_payouts(), [c])
            self.assertEqual(recovered.status(c)["state"], "SETTLED")
            self.assertEqual(self.backend.payouts, [])
            store.close()


class TestStoreMigration(unittest.TestCase):
    def test_legacy_database_gets_held_entry_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "legacy.db")
            conn = sqlite3.connect(path)
            conn.execute("""CREATE TABLE deposits (
                commitment TEXT PRIMARY KEY, amount_sats INTEGER NOT NULL,
                pubkey TEXT NOT NULL, state TEXT NOT NULL, invoice TEXT,
                invoice_label TEXT, created_at INTEGER NOT NULL, paid_at INTEGER,
                release_at INTEGER, offer TEXT, offer_hash TEXT, fee_bps INTEGER,
                payout_sats INTEGER, preimage TEXT
            )""")
            conn.commit()
            conn.close()
            store = Store(path)
            columns = {
                row[1] for row in store._conn.execute("PRAGMA table_info(deposits)")
            }
            self.assertTrue({
                "payment_hash", "entry_preimage", "entry_state",
                "entry_expiry", "entry_settled_at", "nullifier_hash",
                "authorization_digest",
            }.issubset(columns))
            store.close()


class TestCLNHeldRPC(unittest.TestCase):
    def test_creation_injects_before_returning_invoice(self):
        backend = CLNBackend("/tmp/unused", held_htlc=True)
        calls = []

        def fake_call(method, **params):
            calls.append((method, params))
            if method == "invoice":
                return {"bolt11": "lnbcrt1privateuntilinjected"}
            if method == "injectholdinvoice":
                return {}
            raise AssertionError(method)
        backend._call = fake_call
        created = backend.create_held_invoice(Q, "label", "description", 7200)
        self.assertEqual([c[0] for c in calls], ["invoice", "injectholdinvoice"])
        self.assertEqual(created.bolt11, "lnbcrt1privateuntilinjected")
        self.assertEqual(
            created.payment_hash,
            hashlib.sha256(bytes.fromhex(created.preimage)).hexdigest(),
        )
        self.assertEqual(calls[0][1]["expiry"], 7200)
        self.assertEqual(calls[0][1]["preimage"], created.preimage)

    def test_hold_plugin_status_parses_lowercase_and_iso_time(self):
        backend = CLNBackend("/tmp/unused", held_htlc=True)
        backend._call = lambda *args, **kwargs: {
            "holdinvoices": [{
                "state": "accepted", "created_at": "2026-10-26T10:00:00",
                "htlcs": [{
                    "created_at": "2026-10-26T10:01:02", "cltv_expiry": 12345,
                }],
            }],
        }
        status = backend.held_invoice_status("ab" * 32)
        self.assertEqual(status.state, "accepted")
        self.assertEqual(status.accepted_at, 1793008862)
        self.assertEqual(status.htlc_expiry, 12345)


if __name__ == "__main__":
    unittest.main()
