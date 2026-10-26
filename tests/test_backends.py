"""Asset-backend selection, capability and fail-closed RGB tests."""

import os
import secrets
import unittest
from unittest.mock import patch

from app import lattice, schnorr
from app.lightning import BackendError, CLNBackend, MockBackend, from_env
from app.rgb import RGBBackend
from app.service import Coordinator, ServiceError
from app.store import Store


class TestBackendSelection(unittest.TestCase):
    def test_default_is_btc_mock(self):
        with patch.dict(os.environ, {}, clear=True):
            backend = from_env()
        self.assertIsInstance(backend, MockBackend)
        self.assertEqual(backend.spec.asset_id, "btc")
        self.assertEqual(backend.spec.amount_unit, "sats")
        self.assertTrue(backend.spec.enabled)

    def test_btc_cln_selection_is_preserved(self):
        env = {"SWIRL_ASSET_BACKEND": "btc", "SWIRL_CLN_RPC": "/tmp/cln-rpc"}
        with patch.dict(os.environ, env, clear=True):
            backend = from_env()
        self.assertIsInstance(backend, CLNBackend)
        self.assertEqual(backend.rpc_path, "/tmp/cln-rpc")
        self.assertEqual(backend.spec.backend, "btc-cln")

    def test_rgb_selection_returns_disabled_adapter(self):
        env = {"SWIRL_ASSET_BACKEND": "RGB", "SWIRL_RGB_ASSET_ID": "rgb:test"}
        with patch.dict(os.environ, env, clear=True):
            backend = from_env()
        self.assertIsInstance(backend, RGBBackend)
        self.assertEqual(backend.spec.asset_id, "rgb:test")
        self.assertEqual(backend.spec.amount_unit, "asset_base_unit")
        self.assertFalse(backend.spec.enabled)

    def test_unknown_backend_fails_at_startup(self):
        with patch.dict(os.environ, {"SWIRL_ASSET_BACKEND": "usdt"}, clear=True):
            with self.assertRaisesRegex(ValueError, "expected 'btc' or 'rgb'"):
                from_env()


class TestRGBFailClosed(unittest.TestCase):
    def setUp(self):
        with patch.dict(os.environ, {}, clear=True):
            self.backend = RGBBackend()

    def test_every_fund_moving_method_refuses(self):
        calls = (
            lambda: self.backend.create_invoice(1, "label", "description", 60),
            lambda: self.backend.invoice_status("label"),
            lambda: self.backend.invoice_paid("label"),
            lambda: self.backend.pay_offer("lno1test", 1),
            lambda: self.backend.node_uri(),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaisesRegex(BackendError, "refusing all fund-moving"):
                    call()

    def test_coordinator_rejects_deposit_without_creating_state(self):
        store = Store(":memory:")
        coord = Coordinator(store, self.backend)
        pubkey = schnorr.pubkey(secrets.token_bytes(32)).hex()
        with self.assertRaises(ServiceError) as cm:
            coord.create_deposit(lattice.LATTICE[0], pubkey)
        self.assertEqual(cm.exception.code, "asset_backend_unavailable")
        self.assertEqual(cm.exception.http, 503)
        self.assertEqual(store.leaves_in_order(), [])

    def test_params_expose_capabilities_without_btc_lattice(self):
        coord = Coordinator(Store(":memory:"), self.backend)
        params = coord.params()
        self.assertFalse(params["asset_backend"]["enabled"])
        self.assertEqual(params["asset_backend"]["backend"], "rgb")
        self.assertEqual(params["lattice_sats"], [])
        self.assertIn("unavailable", params["settlement"])

    def test_btc_params_remain_compatible(self):
        coord = Coordinator(Store(":memory:"), MockBackend())
        params = coord.params()
        self.assertEqual(params["lattice_sats"], list(lattice.LATTICE))
        self.assertEqual(params["asset_backend"]["asset_id"], "btc")
        self.assertTrue(params["asset_backend"]["enabled"])


if __name__ == "__main__":
    unittest.main()
