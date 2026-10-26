"""Deterministic shared-set rounds across independent coordinators."""

import secrets
import unittest

from app import merkle, receipts, schnorr
from app.federation import FederationConfig
from app.lightning import MockBackend
from app.service import Config, Coordinator, ServiceError
from app.store import Store

Q = 1_000_000
OFFER_A = "lno1-federation-exit-a"
OFFER_B = "lno1-federation-exit-b"


class FederationCase(unittest.TestCase):
    def setUp(self):
        self.backends = {"a": MockBackend(), "b": MockBackend()}
        self.coords = {}
        for name in ("a", "b"):
            cfg = FederationConfig(
                coordinator_id=f"coordinator-{name}",
                peers=(f"http://{name}.test",),
                token="test-federation-token",
                allow_http=True,
            )
            self.coords[name] = Coordinator(
                Store(":memory:"), self.backends[name],
                Config(min_hold=0, jitter_max=0),
                randbelow=lambda n: 0,
                federation_cfg=cfg,
                snapshot_fetcher=self.fetch_snapshot,
            )

    def fetch_snapshot(self, peer, cfg):
        del cfg
        name = "b" if peer == "http://a.test" else "a"
        return self.coords[name]._local_snapshot()

    def paid_deposit(self, name):
        sk = secrets.token_bytes(32)
        pk = schnorr.pubkey(sk)
        coord = self.coords[name]
        created = coord.create_deposit(Q, pk.hex())
        commitment = created["commitment"]
        self.backends[name].pay_invoice(f"swirl-{commitment[:40]}")
        coord.poll_invoices()
        self.assertEqual(coord.status(commitment)["state"], "HELD")
        return receipts.encode_receipt(sk, Q), commitment

    def sync_both(self):
        self.assertEqual(self.coords["a"].sync_federation(), [])
        self.assertEqual(self.coords["b"].sync_federation(), [])

    def authorize(self, name, receipt, commitment, offer, root):
        coord = self.coords[name]
        pk, nh, sig = receipts.sign_authorization(
            receipt, commitment, root, offer, 50,
        )
        return coord.authorize_payout(
            commitment, root, nh, pk, offer, 50, sig,
        )

    def test_shared_root_proofs_and_home_coordinator_payouts(self):
        receipt_a, commitment_a = self.paid_deposit("a")
        receipt_b, commitment_b = self.paid_deposit("b")
        self.sync_both()

        set_a = self.coords["a"].anonymity_set()
        set_b = self.coords["b"].anonymity_set()
        self.assertEqual(set_a, set_b)
        self.assertEqual(set_a["size"], 2)
        self.assertEqual(set_a["scope"], "federation")
        self.assertEqual(set_a["coordinator_count"], 2)
        root = set_a["merkle_root"]

        for coord in self.coords.values():
            for commitment in (commitment_a, commitment_b):
                proof = coord.inclusion_proof(commitment)
                path = [(p["sibling"], p["side"]) for p in proof["proof"]]
                self.assertTrue(merkle.verify_path(commitment, path, root))
                self.assertEqual(proof["round_id"], set_a["round_id"])

        self.assertEqual(
            self.authorize("a", receipt_a, commitment_a, OFFER_A, root)["state"],
            "QUEUED",
        )
        self.assertEqual(
            self.authorize("b", receipt_b, commitment_b, OFFER_B, root)["state"],
            "QUEUED",
        )

        # A peer's deposit never becomes payable at the wrong home coordinator.
        with self.assertRaises(ServiceError) as cm:
            self.authorize("b", receipt_a, commitment_a, OFFER_A, root)
        self.assertEqual(cm.exception.code, "unknown_commitment")

    def test_snapshot_authentication_and_tamper_retains_last_good_round(self):
        self.paid_deposit("a")
        self.paid_deposit("b")
        with self.assertRaises(ServiceError) as cm:
            self.coords["a"].federation_snapshot("wrong-token")
        self.assertEqual(cm.exception.http, 401)
        self.assertEqual(
            self.coords["a"].federation_snapshot("test-federation-token")[
                "coordinator_id"
            ],
            "coordinator-a",
        )

        self.sync_both()
        good_round = self.coords["a"].anonymity_set()["round_id"]

        def tampered(peer, cfg):
            snapshot = self.fetch_snapshot(peer, cfg)
            snapshot["merkle_root"] = "00" * 32
            return snapshot

        self.coords["a"]._snapshot_fetcher = tampered
        errors = self.coords["a"].sync_federation()
        self.assertEqual(len(errors), 1)
        self.assertIn("root does not match", errors[0])
        self.assertEqual(
            self.coords["a"].anonymity_set()["round_id"], good_round,
        )

    def test_duplicate_commitment_is_one_shared_set_member(self):
        _, commitment = self.paid_deposit("a")
        local = self.coords["a"]._local_snapshot()

        def duplicate(peer, cfg):
            del peer, cfg
            return {
                "coordinator_id": "coordinator-b",
                "commitments": [commitment],
                "merkle_root": local["merkle_root"],
            }

        self.coords["a"]._snapshot_fetcher = duplicate
        self.assertEqual(self.coords["a"].sync_federation(), [])
        shared = self.coords["a"].anonymity_set()
        self.assertEqual(shared["size"], 1)
        self.assertEqual(shared["coordinator_count"], 2)

    def test_invalid_peer_id_is_rejected(self):
        self.coords["a"]._snapshot_fetcher = lambda peer, cfg: {
            "coordinator_id": "coordinator-a",
            "commitments": [],
            "merkle_root": self.coords["a"].tree.root.hex(),
        }
        errors = self.coords["a"].sync_federation()
        self.assertEqual(errors, ["peer returned the local coordinator id"])
        self.assertEqual(self.coords["a"].anonymity_set()["scope"], "local")


if __name__ == "__main__":
    unittest.main()
