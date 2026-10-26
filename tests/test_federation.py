"""Deterministic shared-set rounds across independent coordinators."""

import secrets
import unittest

from app import federation as fed
from app import merkle, receipts, schnorr
from app.federation import FederationConfig
from app.lightning import MockBackend
from app.service import Config, Coordinator, ServiceError
from app.store import Store

Q = 1_000_000
OFFER_A = "lno1-federation-exit-a"
OFFER_B = "lno1-federation-exit-b"
TOKEN = "test-federation-token"


class FederationCase(unittest.TestCase):
    def setUp(self):
        self.backends = {"a": MockBackend(), "b": MockBackend()}
        self.coords = {}
        for name in ("a", "b"):
            cfg = FederationConfig(
                coordinator_id=f"coordinator-{name}",
                peers=(f"http://{name}.test",),
                token=TOKEN,
                allow_http=True,
            )
            self.coords[name] = Coordinator(
                Store(":memory:"), self.backends[name],
                Config(min_hold=0, jitter_max=0),
                randbelow=lambda n: 0,
                federation_cfg=cfg,
                snapshot_fetcher=self.fetch_snapshot,
                federation_rpc=self.rpc,
            )

    @staticmethod
    def peer_name(peer):
        return "b" if peer == "http://a.test" else "a"

    def fetch_snapshot(self, peer, cfg):
        del cfg
        return self.coords[self.peer_name(peer)]._local_snapshot()

    def rpc(self, peer, path, payload, cfg):
        """In-memory equivalent of post_json, including HTTP error mapping."""
        target = self.coords[self.peer_name(peer)]
        method = {
            "/v1/federation/nullifier/reserve": target.federation_reserve,
            "/v1/federation/nullifier/commit": target.federation_commit,
            "/v1/federation/nullifier/abort": target.federation_abort,
        }[path]
        try:
            return method(cfg.token, payload)
        except ServiceError as exc:
            if exc.http == 409:
                raise fed.FederationConflict(exc.code) from exc
            raise fed.FederationError(exc.code) from exc

    def paid_deposit(self, name, receipt=None):
        if receipt is None:
            receipt = receipts.encode_receipt(secrets.token_bytes(32), Q)
        sk, _ = receipts.decode_receipt(receipt)
        pk = schnorr.pubkey(sk)
        coord = self.coords[name]
        created = coord.create_deposit(Q, pk.hex())
        commitment = created["commitment"]
        self.backends[name].pay_invoice(f"swirl-{commitment[:40]}")
        coord.poll_invoices()
        self.assertEqual(coord.status(commitment)["state"], "HELD")
        return receipt, commitment

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

    def nullifier_state(self, name, receipt):
        sk, _ = receipts.decode_receipt(receipt)
        nh = receipts.nullifier_hash_for(sk)
        row = self.coords[name].store._q(
            "SELECT state FROM federation_nullifiers WHERE nullifier_hash=?",
            (nh,), "one",
        )
        return row["state"] if row else None

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
        for name, receipt in (("a", receipt_a), ("b", receipt_b)):
            self.assertEqual(self.nullifier_state(name, receipt), "COMMITTED")
        self.assertEqual(self.nullifier_state("b", receipt_a), "COMMITTED")
        self.assertEqual(self.nullifier_state("a", receipt_b), "COMMITTED")

        # A peer's deposit never becomes payable at the wrong home coordinator.
        with self.assertRaises(ServiceError) as cm:
            self.authorize("b", receipt_a, commitment_a, OFFER_A, root)
        self.assertEqual(cm.exception.code, "unknown_commitment")

    def test_same_nullifier_cannot_queue_at_two_home_coordinators(self):
        receipt, commitment = self.paid_deposit("a")
        self.paid_deposit("b", receipt)
        self.sync_both()
        root = self.coords["a"].anonymity_set()["merkle_root"]

        self.authorize("a", receipt, commitment, OFFER_A, root)
        with self.assertRaises(ServiceError) as cm:
            self.authorize("b", receipt, commitment, OFFER_B, root)
        self.assertEqual(cm.exception.code, "already_spent")
        self.assertEqual(self.coords["a"].status(commitment)["state"], "QUEUED")
        self.assertEqual(self.coords["b"].status(commitment)["state"], "HELD")

    def test_offline_peer_aborts_without_queueing(self):
        receipt, commitment = self.paid_deposit("a")
        self.paid_deposit("b")
        self.sync_both()
        root = self.coords["a"].anonymity_set()["merkle_root"]

        def offline(peer, path, payload, cfg):
            raise fed.FederationError("peer offline")

        self.coords["a"]._federation_rpc = offline
        with self.assertRaises(ServiceError) as cm:
            self.authorize("a", receipt, commitment, OFFER_A, root)
        self.assertEqual(cm.exception.code, "federation_authorization_failed")
        self.assertEqual(self.coords["a"].status(commitment)["state"], "HELD")
        self.assertIsNone(self.nullifier_state("a", receipt))
        self.assertIsNone(self.nullifier_state("b", receipt))

    def test_lost_reserve_response_still_aborts_peer_reservation(self):
        receipt, commitment = self.paid_deposit("a")
        self.paid_deposit("b")
        self.sync_both()
        root = self.coords["a"].anonymity_set()["merkle_root"]

        def lost_response(peer, path, payload, cfg):
            result = self.rpc(peer, path, payload, cfg)
            if path.endswith("/reserve"):
                raise fed.FederationError("response lost after durable reserve")
            return result

        self.coords["a"]._federation_rpc = lost_response
        with self.assertRaises(ServiceError) as cm:
            self.authorize("a", receipt, commitment, OFFER_A, root)
        self.assertEqual(cm.exception.code, "federation_authorization_failed")
        self.assertEqual(self.coords["a"].status(commitment)["state"], "HELD")
        self.assertIsNone(self.nullifier_state("a", receipt))
        self.assertIsNone(self.nullifier_state("b", receipt))

    def test_commit_failure_retries_without_rolling_back_queue(self):
        receipt, commitment = self.paid_deposit("a")
        self.paid_deposit("b")
        self.sync_both()
        root = self.coords["a"].anonymity_set()["merkle_root"]
        failed = []

        def flaky_commit(peer, path, payload, cfg):
            if path.endswith("/commit") and not failed:
                failed.append(peer)
                raise fed.FederationError("temporary commit failure")
            return self.rpc(peer, path, payload, cfg)

        self.coords["a"]._federation_rpc = flaky_commit
        self.assertEqual(
            self.authorize("a", receipt, commitment, OFFER_A, root)["state"],
            "QUEUED",
        )
        self.assertEqual(self.nullifier_state("a", receipt), "COMMITTED")
        self.assertEqual(self.nullifier_state("b", receipt), "RESERVED")

        self.coords["a"]._federation_rpc = self.rpc
        self.assertEqual(self.coords["a"].commit_federation_nullifiers(), [])
        self.assertEqual(self.nullifier_state("b", receipt), "COMMITTED")
        self.assertEqual(self.coords["a"].status(commitment)["state"], "QUEUED")

    def test_stale_root_bad_signature_and_bad_proof_are_rejected(self):
        receipt, commitment = self.paid_deposit("a")
        self.sync_both()
        old_root = self.coords["a"].anonymity_set()["merkle_root"]
        self.paid_deposit("b")
        self.sync_both()
        root = self.coords["a"].anonymity_set()["merkle_root"]

        with self.assertRaises(ServiceError) as cm:
            self.authorize("a", receipt, commitment, OFFER_A, old_root)
        self.assertEqual(cm.exception.code, "federation_root_stale")

        pk, nh, sig = receipts.sign_authorization(
            receipt, commitment, root, OFFER_A, 50,
        )
        with self.assertRaises(ServiceError) as cm:
            self.coords["a"].authorize_payout(
                commitment, root, nh, pk, OFFER_A, 50, "00" * 64,
            )
        self.assertEqual(cm.exception.code, "bad_signature")

        proof = self.coords["a"].inclusion_proof(commitment)
        payload = {
            "commitment": commitment, "root": root, "nullifier_hash": nh,
            "pubkey": pk, "offer": OFFER_A, "amount_sats": Q, "fee_bps": 50,
            "signature": sig, "proof": proof["proof"], "origin": "coordinator-a",
        }
        payload["proof"][0]["sibling"] = "00" * 32
        with self.assertRaises(ServiceError) as cm:
            self.coords["b"].federation_reserve(TOKEN, payload)
        self.assertEqual(cm.exception.code, "bad_federation_proof")
        self.assertEqual(cm.exception.http, 422)
        self.assertEqual(self.coords["a"].status(commitment)["state"], "HELD")

    def test_federation_transition_requires_valid_token_and_fields(self):
        payload = {
            "nullifier_hash": "11" * 32,
            "authorization_digest": "22" * 32,
            "origin": "coordinator-a",
        }
        with self.assertRaises(ServiceError) as cm:
            self.coords["b"].federation_commit("wrong-token", payload)
        self.assertEqual(cm.exception.code, "federation_unauthorized")
        payload["nullifier_hash"] = "not-hex"
        with self.assertRaises(ServiceError) as cm:
            self.coords["b"].federation_commit(TOKEN, payload)
        self.assertEqual(cm.exception.code, "bad_federation_request")
        self.assertEqual(cm.exception.http, 422)

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
