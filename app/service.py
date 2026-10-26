"""Coordinator service — the protocol state machine.

    AWAITING_PAYMENT ──invoice paid──▶ HELD ──signed payout auth──▶ QUEUED
         │                                  (60 min hold + jitter)     │
         └─expired─▶ EXPIRED                              SETTLING ─▶ SETTLED

Enforced here, not in documentation:
  * lattice-only amounts                       (422 lattice_violation)
  * min-hold + per-deposit random jitter       (release_at)
  * single-use BOLT12 exit offers              (409 offer_reuse)
  * fee + destination bound in the signature   (401 bad_signature)
  * atomic state transitions against double-pay
"""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass

from . import lattice, merkle, receipts
from .lightning import BackendError
from .store import Store


class ServiceError(Exception):
    def __init__(self, code: str, message: str, http: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


@dataclass
class Config:
    min_hold: int = 3600        # seconds, per chunk
    jitter_max: int = 900       # additional random delay, per chunk
    invoice_expiry: int = 3600
    fee_bps: int = lattice.FEE_BPS


class Coordinator:
    def __init__(self, store: Store, backend, config: Config | None = None,
                 randbelow=secrets.randbelow, clock=time.time):
        self.store = store
        self.backend = backend
        self.cfg = config or Config()
        self._randbelow = randbelow
        self._clock = clock

    # ── public params ────────────────────────────────────────
    def params(self) -> dict:
        return {
            "version": "0.2.0",
            "lattice_sats": list(lattice.LATTICE),
            "fee_bps": self.cfg.fee_bps,
            "min_hold_seconds": self.cfg.min_hold,
            "exit_jitter_max_seconds": self.cfg.jitter_max,
            "invoice_expiry_seconds": self.cfg.invoice_expiry,
            "settlement": "BOLT12 blinded paths, single-use offers",
        }

    # ── deposit ──────────────────────────────────────────────
    def create_deposit(self, amount_sats: int, pubkey_hex: str) -> dict:
        try:
            lattice.check_quantum(amount_sats)
        except lattice.LatticeViolation as e:
            raise ServiceError("lattice_violation", str(e), 422) from e
        if len(pubkey_hex) != 64:
            raise ServiceError("bad_pubkey", "x-only pubkey must be 32 bytes hex", 422)
        try:
            pub = bytes.fromhex(pubkey_hex)
        except ValueError as e:
            raise ServiceError("bad_pubkey", "pubkey must be hex", 422) from e

        commitment = receipts.make_commitment(pub, amount_sats)
        if self.store.get(commitment):
            raise ServiceError("duplicate_commitment",
                               "generate a fresh receipt key for every deposit", 409)
        now = int(self._clock())
        label = f"swirl-{commitment[:40]}"
        invoice = self.backend.create_invoice(
            amount_sats, label, f"SWIRL pool deposit {amount_sats} sats",
            self.cfg.invoice_expiry,
        )
        self.store.insert_deposit({
            "commitment": commitment, "amount_sats": amount_sats,
            "pubkey": pubkey_hex, "state": "AWAITING_PAYMENT",
            "invoice": invoice, "invoice_label": label, "created_at": now,
        })
        return {
            "commitment": commitment,
            "invoice": invoice,
            "amount_sats": amount_sats,
            "expires_at": now + self.cfg.invoice_expiry,
            "min_hold_seconds": self.cfg.min_hold,
        }

    def poll_invoices(self):
        """Worker step: expire stale invoices, mark paid ones HELD."""
        now = int(self._clock())
        for d in self.store.by_state("AWAITING_PAYMENT"):
            if now > d["created_at"] + self.cfg.invoice_expiry:
                self.store.transition(d["commitment"], "AWAITING_PAYMENT", "EXPIRED")
                continue
            status = self.backend.invoice_status(d["invoice_label"])
            if status[0]:
                # hold starts at the node's real payment time, not poll time
                self.store.transition(
                    d["commitment"], "AWAITING_PAYMENT", "HELD",
                    paid_at=status[1] or now,
                )

    # ── payout authorization ─────────────────────────────────
    def authorize_payout(self, commitment: str, pubkey_hex: str, offer: str,
                         fee_bps: int, signature_hex: str) -> dict:
        d = self.store.get(commitment)
        if not d:
            raise ServiceError("unknown_commitment", "no such deposit", 404)
        if d["state"] not in ("HELD", "QUEUED"):
            raise ServiceError("not_payable", f"deposit is {d['state']}", 409)
        if d["state"] == "QUEUED":
            raise ServiceError("already_queued", "payout already authorized", 409)
        if not offer or len(offer) > 1024 or any(c.isspace() for c in offer):
            raise ServiceError("bad_offer", "invalid BOLT12 offer", 422)
        if fee_bps != self.cfg.fee_bps:
            raise ServiceError("fee_mismatch",
                               f"current fee is {self.cfg.fee_bps} bps", 422)
        if not receipts.verify_authorization(
            commitment, pubkey_hex, signature_hex, offer, d["amount_sats"], fee_bps
        ):
            raise ServiceError("bad_signature",
                               "authorization does not match commitment/offer/fee", 401)

        oh = receipts.offer_hash(offer)
        jitter = self._randbelow(self.cfg.jitter_max + 1) if self.cfg.jitter_max else 0
        release_at = d["paid_at"] + self.cfg.min_hold + jitter
        payout = lattice.payout_amount(d["amount_sats"])
        result = self.store.queue_payout(
            commitment, offer, oh, release_at, fee_bps, payout,
        )
        if result == "offer_reuse":
            raise ServiceError("offer_reuse",
                               "every exit needs a fresh BOLT12 offer", 409)
        if result != "ok":
            raise ServiceError("already_queued", "payout already authorized", 409)
        return {
            "commitment": commitment,
            "state": "QUEUED",
            "release_at": release_at,
            "payout_amount_sats": payout,
            "fee_sats": lattice.fee_for(d["amount_sats"]),
        }

    # ── settlement journal (crash recovery) ─────────────────
    # If the DB write fails AFTER the node paid, the preimage must not die
    # with the process. Every successful payment is appended to an
    # append-only journal BEFORE the state flip; on the next worker round
    # any SETTLING row found in the journal is reconciled to SETTLED.
    @property
    def _journal_path(self) -> str | None:
        return None if self.store.path == ":memory:" else self.store.path + ".settlements"

    def _journal_append(self, entry: dict):
        path = self._journal_path
        if not path:
            return
        import json
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _journal_preimages(self) -> dict[str, str]:
        path = self._journal_path
        out: dict[str, str] = {}
        if not path or not os.path.exists(path):
            return out
        import json
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    out[e["commitment"]] = e["preimage"]
                except (ValueError, KeyError):
                    continue
        return out

    def reconcile_settling(self):
        """Recover payouts that settled on the node but not in the DB.
        Rows still in SETTLING without a journal entry are left untouched —
        reconciled against listpays by an operator, never blindly repaid."""
        journal = self._journal_preimages()
        for d in self.store.by_state("SETTLING"):
            preimage = journal.get(d["commitment"])
            if preimage:
                self.store.transition(
                    d["commitment"], "SETTLING", "SETTLED",
                    preimage=preimage, offer=None,
                )
                print(f"reconciled {d['commitment']} from settlement journal", flush=True)

    def execute_due_payouts(self):
        """Worker step: settle every QUEUED deposit whose hold has elapsed.
        Plaintext offers are read from the queue row and purged on settlement.
        """
        self.reconcile_settling()
        now = int(self._clock())
        settled = []
        for d in self.store.by_state("QUEUED"):
            if d["release_at"] is None or now < d["release_at"]:
                continue
            if not self.store.transition(d["commitment"], "QUEUED", "SETTLING"):
                continue
            try:
                if not d["offer"]:
                    raise BackendError("no offer available for payout")
                preimage = self.backend.pay_offer(d["offer"], d["payout_sats"])
            except Exception:
                # the payment did not go through — safe to retry next round
                self.store.transition(d["commitment"], "SETTLING", "QUEUED")
                continue
            # journal first: even if the DB write now fails, the preimage
            # survives and reconcile_settling() will finish the job
            self._journal_append({
                "commitment": d["commitment"], "preimage": preimage,
                "amount_sats": d["payout_sats"], "at": now,
            })
            self.store.transition(
                d["commitment"], "SETTLING", "SETTLED",
                preimage=preimage, offer=None,
            )
            settled.append(d["commitment"])
        return settled

    # ── read-only views ──────────────────────────────────────
    def status(self, commitment: str) -> dict:
        d = self.store.get(commitment)
        if not d:
            raise ServiceError("unknown_commitment", "no such deposit", 404)
        return {
            "commitment": commitment,
            "state": d["state"],
            "amount_sats": d["amount_sats"],
            "created_at": d["created_at"],
            "paid_at": d["paid_at"],
            "release_at": d["release_at"],
            "payout_amount_sats": d["payout_sats"],
        }

    def anonymity_set(self) -> dict:
        live = self.store.live_commitments()
        root, _ = merkle.build(live)
        return {"size": len(live), "merkle_root": root.hex()}

    def inclusion_proof(self, commitment: str) -> dict:
        live = self.store.live_commitments()
        prf = merkle.proof(live, commitment)
        if prf is None:
            raise ServiceError("not_in_set", "commitment is not in the live set", 404)
        root, _ = merkle.build(live)
        return {
            "merkle_root": root.hex(),
            "proof": [{"sibling": s, "side": side} for s, side in prf],
        }
