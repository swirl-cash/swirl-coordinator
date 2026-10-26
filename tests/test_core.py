"""Protocol test-suite — stdlib unittest, no dependencies:
    python -m unittest discover -s tests -v
"""

import secrets
import unittest

from app import lattice, merkle, receipts, schnorr
from app.lightning import MockBackend
from app.service import Coordinator, Config, ServiceError
from app.store import Store

Q = 1_000_000  # 0.01 BTC
OFFER1 = "lno1qgsyxj6q9p8qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq"
OFFER2 = "lno1qgsyxj6q9p8qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqr"


class TestLattice(unittest.TestCase):
    def test_quanta_accepted(self):
        for q in lattice.LATTICE:
            self.assertEqual(lattice.check_quantum(q), q)

    def test_off_lattice_rejected(self):
        for bad in (0, 1, 1873340, Q + 1, -5, 0.5, True, "100000"):
            with self.assertRaises(lattice.LatticeViolation):
                lattice.check_quantum(bad)  # type: ignore[arg-type]

    def test_decompose(self):
        self.assertEqual(lattice.decompose(11_100_000),
                         [10_000_000, 1_000_000, 100_000])
        # anything below the smallest quantum comes out as residual, never a chunk
        self.assertEqual(lattice.decompose(111_000), [100_000, 11_000])
        self.assertEqual(lattice.decompose(0), [])
        self.assertEqual(sum(lattice.decompose(1873340)), 1873340)

    def test_fee(self):
        self.assertEqual(lattice.fee_for(Q), 5_000)          # 50 bps
        self.assertEqual(lattice.payout_amount(Q), Q - 5_000)


# Official BIP340 test vectors (bitcoin/bips, bip-0340/test-vectors.csv)
BIP340_SIGN_VECTORS = [
    ("0000000000000000000000000000000000000000000000000000000000000003",
     "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "0000000000000000000000000000000000000000000000000000000000000000",
     "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0"),
    ("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
     "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "0000000000000000000000000000000000000000000000000000000000000001",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE33418906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A"),
    ("C90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9",
     "DD308AFEC5777E13121FA72B9CC1B7CC0139715309B086C960E18FD969774EB8",
     "C87AA53824B4D7AE2EB035A2B5BBBCCC080E76CDC6D1692C4B0B62D798E6D906",
     "7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C",
     "5831AAEED7B44BB74E5EAB94BA9D4294C49BCF2A60728D8B4C200F50DD313C1BAB745879A5AD954A72C45A91C3A51D3C7ADEA98D82F8481E0E1E03674A6F3FB7"),
    ("0B432B2677937381AEF05BB02A66ECD012773062CF3FA2549E44F58ED2401710",
     "25D1DFF95105F5253C4022F628A996AD3A0D95FBF21D468A1B33F8C160D8F517",
     "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
     "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
     "7EB0509757E246F19449885651611CB965ECC1A187DD51B64FDA1EDC9637D5EC97582B9CB13DB3933705B32BA982AF5AF25FD78881EBB32771FC5922EFC66EA3"),
]

BIP340_VERIFY_VECTORS = [  # (pub, msg, sig, expected)
    ("D69C3509BB99E412E68B0FE8544E72837DFA30746D8BE2AA65975F29D22DC7B9",
     "4DF3C3F68FCC83B27E9D42C90431A72499F17875C81A599B566C9889B9696703",
     "00000000000000000000003B78CE563F89A0ED9414F5AA28AD0D96D6795F9C6376AFB1548AF603B3EB45C9F8207DEE1060CB71C04E80F593060B07D28308D7F4", True),
    ("EEFDEA4CDB677750A420FEE807EACF21EB9898AE79B9768766E4FAA04A2D4A34",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E17776969E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B", False),
    ("DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "FFF97BD5755EEEA420453A14355235D382F6472F8568A18B2F057A14602975563CC27944640AC607CD107AE10923D9EF7A73C643E166BE5EBEAFA34B1AC553E2", False),
    ("DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "1FA62E331EDBC21C394792D2AB1100A7B432B013DF3F6FF4F99FCB33E0E1515F28890B3EDB6E7189B630448B515CE4F8622A954CFE545735AAEA5134FCCDB2BD", False),
    ("DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "0000000000000000000000000000000000000000000000000000000000000000123DDA8328AF9C23A94C1FEECFD123BA4FB73476F0D594DCB65C6425BD186051", False),
    ("DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "4A298DACAE57395A15D0795DDBFD1DCB564DA82B0F269BC70A74F8220429BA1D69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B", False),
    ("DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B", False),
    ("DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E177769FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", False),
    ("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC30",
     "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
     "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E17776969E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B", False),
]


class TestSchnorr(unittest.TestCase):
    def test_bip340_official_sign_vectors(self):
        for sk, pk, aux, msg, sig in BIP340_SIGN_VECTORS:
            sk_b, msg_b = bytes.fromhex(sk), bytes.fromhex(msg)
            self.assertEqual(schnorr.pubkey(sk_b).hex().upper(), pk)
            self.assertEqual(
                schnorr.sign(msg_b, sk_b, aux_rand=bytes.fromhex(aux)).hex().upper(),
                sig,
            )
            self.assertTrue(schnorr.verify(msg_b, bytes.fromhex(pk), bytes.fromhex(sig)))

    def test_bip340_official_verify_vectors(self):
        for pk, msg, sig, expected in BIP340_VERIFY_VECTORS:
            self.assertEqual(
                schnorr.verify(bytes.fromhex(msg), bytes.fromhex(pk), bytes.fromhex(sig)),
                expected,
            )

    def test_roundtrip_and_tamper(self):
        sk = secrets.token_bytes(32)
        pk = schnorr.pubkey(sk)
        msg = secrets.token_bytes(32)
        sig = schnorr.sign(msg, sk)
        self.assertTrue(schnorr.verify(msg, pk, sig))
        self.assertFalse(schnorr.verify(secrets.token_bytes(32), pk, sig))
        bad = bytearray(sig)
        bad[10] ^= 1
        self.assertFalse(schnorr.verify(msg, pk, bytes(bad)))
        self.assertFalse(schnorr.verify(msg, secrets.token_bytes(32), sig))


class TestReceipts(unittest.TestCase):
    def setUp(self):
        self.sk = secrets.token_bytes(32)
        self.pk = schnorr.pubkey(self.sk)
        self.commitment = receipts.make_commitment(self.pk, Q)
        self.receipt = receipts.encode_receipt(self.sk, Q)

    def test_encode_decode(self):
        sk, amount = receipts.decode_receipt(self.receipt)
        self.assertEqual((sk, amount), (self.sk, Q))
        with self.assertRaises(ValueError):
            receipts.decode_receipt("SWIRL1-not-a-receipt")

    def test_authorization_roundtrip(self):
        pk_hex, sig = receipts.sign_authorization(
            self.receipt, self.commitment, OFFER1, 50)
        self.assertTrue(receipts.verify_authorization(
            self.commitment, pk_hex, sig, OFFER1, Q, 50))

    def test_fee_and_offer_are_bound(self):
        pk_hex, sig = receipts.sign_authorization(
            self.receipt, self.commitment, OFFER1, 50)
        # raising the fee after the fact breaks the signature
        self.assertFalse(receipts.verify_authorization(
            self.commitment, pk_hex, sig, OFFER1, Q, 500))
        # swapping the destination offer breaks it too
        self.assertFalse(receipts.verify_authorization(
            self.commitment, pk_hex, sig, OFFER2, Q, 50))

    def test_receipt_commitment_mismatch(self):
        with self.assertRaises(ValueError):
            receipts.sign_authorization(self.receipt, "00" * 32, OFFER1, 50)


class TestMerkle(unittest.TestCase):
    def test_proofs(self):
        commitments = [secrets.token_bytes(32).hex() for _ in range(9)]
        for size in (0, 1, 2, 3, 8, 9):
            subset = commitments[:size]
            root, _ = merkle.build(subset)
            for c in subset:
                prf = merkle.proof(subset, c)
                self.assertIsNotNone(prf)
                self.assertTrue(merkle.verify_proof(c, prf, root.hex()))

    def test_tampered_proof_fails(self):
        cs = [secrets.token_bytes(32).hex() for _ in range(4)]
        root, _ = merkle.build(cs)
        prf = merkle.proof(cs, cs[1])
        prf[0] = (secrets.token_bytes(32).hex(), prf[0][1])
        self.assertFalse(merkle.verify_proof(cs[1], prf, root.hex()))
        self.assertIsNone(merkle.proof(cs, "ab" * 32))


class FakeClock:
    def __init__(self):
        self.t = 1_700_000_000

    def __call__(self):
        return self.t


class TestCoordinator(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.backend = MockBackend()
        self.coord = Coordinator(
            Store(":memory:"), self.backend,
            Config(min_hold=3600, jitter_max=900),
            randbelow=lambda n: 0, clock=self.clock,
        )
        self.sk = secrets.token_bytes(32)
        self.pk_hex = schnorr.pubkey(self.sk).hex()
        self.receipt = receipts.encode_receipt(self.sk, Q)

    def _deposit(self, amount=Q, pay=True):
        res = self.coord.create_deposit(amount, self.pk_hex)
        if pay:
            label = f"swirl-{res['commitment'][:40]}"
            self.backend.pay_invoice(label)
            self.coord.poll_invoices()
        return res

    def _authorize(self, commitment, offer=OFFER1, fee=50, receipt=None):
        pk, sig = receipts.sign_authorization(
            receipt or self.receipt, commitment, offer, fee)
        return self.coord.authorize_payout(commitment, pk, offer, fee, sig)

    def test_full_flow(self):
        res = self._deposit(pay=False)
        c = res["commitment"]
        self.assertTrue(res["invoice"].startswith("mockinv1"))
        self.assertEqual(self.coord.status(c)["state"], "AWAITING_PAYMENT")

        # payout before the invoice is paid is impossible
        with self.assertRaises(ServiceError) as cm:
            self._authorize(c)
        self.assertEqual(cm.exception.code, "not_payable")

        label = f"swirl-{c[:40]}"
        self.backend.pay_invoice(label)
        self.coord.poll_invoices()
        self.assertEqual(self.coord.status(c)["state"], "HELD")

        auth = self._authorize(c)
        self.assertEqual(auth["state"], "QUEUED")
        self.assertEqual(auth["payout_amount_sats"], Q - 5_000)
        self.assertEqual(auth["release_at"],
                         self.coord.status(c)["paid_at"] + 3600)

        # min-hold: nothing settles early
        self.assertEqual(self.coord.execute_due_payouts(), [])
        self.clock.t += 3599
        self.assertEqual(self.coord.execute_due_payouts(), [])
        self.clock.t += 2
        self.assertEqual(self.coord.execute_due_payouts(), [c])
        st = self.coord.status(c)
        self.assertEqual(st["state"], "SETTLED")
        self.assertEqual(self.backend.payouts[0]["amount_sats"], Q - 5_000)
        # plaintext offer is purged after settlement
        self.assertIsNone(self.coord.store.get(c)["offer"])
        # idempotent: a second round settles nothing again
        self.assertEqual(self.coord.execute_due_payouts(), [])

    def test_lattice_violation(self):
        with self.assertRaises(ServiceError) as cm:
            self.coord.create_deposit(1873340, self.pk_hex)
        self.assertEqual(cm.exception.code, "lattice_violation")
        self.assertEqual(cm.exception.http, 422)

    def test_duplicate_commitment(self):
        self._deposit()
        with self.assertRaises(ServiceError) as cm:
            self.coord.create_deposit(Q, self.pk_hex)
        self.assertEqual(cm.exception.code, "duplicate_commitment")

    def test_bad_signature(self):
        res = self._deposit()
        pk, sig = receipts.sign_authorization(self.receipt, res["commitment"], OFFER1, 50)
        with self.assertRaises(ServiceError) as cm:
            self.coord.authorize_payout(res["commitment"], pk, OFFER1, 500, sig)
        self.assertEqual(cm.exception.code, "fee_mismatch")
        with self.assertRaises(ServiceError) as cm:
            self.coord.authorize_payout(res["commitment"], pk, OFFER2, 50, sig)
        self.assertEqual(cm.exception.code, "bad_signature")
        self.assertEqual(cm.exception.http, 401)

    def test_offer_single_use(self):
        c1 = self._deposit()["commitment"]
        self._authorize(c1, offer=OFFER1)
        sk2 = secrets.token_bytes(32)
        receipt2 = receipts.encode_receipt(sk2, Q)
        res2 = self.coord.create_deposit(Q, schnorr.pubkey(sk2).hex())
        self.backend.pay_invoice(f"swirl-{res2['commitment'][:40]}")
        self.coord.poll_invoices()
        with self.assertRaises(ServiceError) as cm:
            self._authorize(res2["commitment"], offer=OFFER1, receipt=receipt2)
        self.assertEqual(cm.exception.code, "offer_reuse")
        self.assertEqual(cm.exception.http, 409)
        # a fresh offer works
        self._authorize(res2["commitment"], offer=OFFER2, receipt=receipt2)

    def test_invoice_expiry(self):
        res = self._deposit(pay=False)
        self.clock.t += 3601
        self.coord.poll_invoices()
        self.assertEqual(self.coord.status(res["commitment"])["state"], "EXPIRED")

    def test_anonymity_set_and_proof(self):
        res = self._deposit()
        c = res["commitment"]
        s = self.coord.anonymity_set()
        self.assertEqual(s["size"], 1)
        prf = self.coord.inclusion_proof(c)
        flat = [(p["sibling"], p["side"]) for p in prf["proof"]]
        self.assertTrue(merkle.verify_proof(c, flat, prf["merkle_root"]))
        with self.assertRaises(ServiceError):
            self.coord.inclusion_proof("ab" * 32)

    def test_failed_payout_retries_not_settles(self):
        res = self._deposit()
        c = res["commitment"]
        self._authorize(c)
        self.clock.t += 3601

        def boom(offer, amount):
            raise RuntimeError("node unreachable")
        self.backend.pay_offer = boom
        self.assertEqual(self.coord.execute_due_payouts(), [])
        self.assertEqual(self.coord.status(c)["state"], "QUEUED")
        self.backend.pay_offer = lambda offer, amount: "aa" * 32
        self.assertEqual(self.coord.execute_due_payouts(), [c])


if __name__ == "__main__":
    unittest.main()
