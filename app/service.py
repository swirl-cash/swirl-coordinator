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

from . import __version__, lattice, merkle, receipts
from .assets import AssetBackend
from .lightning import BackendError, PaymentAmbiguous
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
    settlement_safety_seconds: int = 900
    settlement_safety_blocks: int = 12


class Coordinator:
    def __init__(self, store: Store, backend: AssetBackend,
                 config: Config | None = None,
                 randbelow=secrets.randbelow, clock=time.time):
        self.store = store
        self.backend = backend
        self.cfg = config or Config()
        self.held_invoices = backend.held_invoices_enabled
        self._randbelow = randbelow
        self._clock = clock
        required = (
            self.cfg.min_hold + self.cfg.jitter_max
            + self.cfg.settlement_safety_seconds
        )
        if self.held_invoices and self.cfg.invoice_expiry <= required:
            raise ValueError(
                "held HTLC invoice_expiry must exceed min_hold + jitter_max "
                "+ settlement_safety_seconds"
            )
        # permanent append-only tree, replayed from the leaf table on boot
        self.tree = merkle.AppendOnlyTree()
        for c in self.store.leaves_in_order():
            self.tree.insert(c)

    def _require_enabled(self):
        if not self.backend.spec.enabled:
            raise ServiceError(
                "asset_backend_unavailable", self.backend.spec.detail, 503,
            )

    @staticmethod
    def _is_held(d: dict) -> bool:
        return bool(d.get("payment_hash"))

    # ── public params ────────────────────────────────────────
    def params(self) -> dict:
        is_btc = self.backend.spec.asset_id == "btc"
        return {
            "version": __version__,
            "asset_backend": self.backend.spec.public(),
            "lattice_sats": list(lattice.LATTICE) if is_btc else [],
            "fee_bps": self.cfg.fee_bps,
            "min_hold_seconds": self.cfg.min_hold,
            "exit_jitter_max_seconds": self.cfg.jitter_max,
            "invoice_expiry_seconds": self.cfg.invoice_expiry,
            "entry_mode": "held_htlc" if self.held_invoices else "standard",
            "settlement_safety_blocks": self.cfg.settlement_safety_blocks,
            "settlement": self.backend.spec.settlement,
        }

    # ── deposit ──────────────────────────────────────────────
    def create_deposit(self, amount_sats: int, pubkey_hex: str) -> dict:
        self._require_enabled()
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
        description = f"SWIRL pool deposit {amount_sats} sats"
        held = None
        if self.held_invoices:
            held = self.backend.create_held_invoice(
                amount_sats, label, description, self.cfg.invoice_expiry,
            )
            invoice = held.bolt11
        else:
            invoice = self.backend.create_invoice(
                amount_sats, label, description, self.cfg.invoice_expiry,
            )
        self.store.insert_deposit({
            "commitment": commitment, "amount_sats": amount_sats,
            "pubkey": pubkey_hex, "state": "AWAITING_PAYMENT",
            "invoice": invoice, "invoice_label": label, "created_at": now,
            "payment_hash": held.payment_hash if held else None,
            "entry_preimage": held.preimage if held else None,
            "entry_state": "unpaid" if held else None,
        })
        leaf_index, root = self.tree.insert(commitment)
        self.store.add_leaf(commitment, amount_sats, leaf_index, root)
        return {
            "commitment": commitment,
            "invoice": invoice,
            "amount_sats": amount_sats,
            "leaf_index": leaf_index,
            "merkle_root": root,
            "entry_mode": "held_htlc" if held else "standard",
            "payment_hash": held.payment_hash if held else None,
            "expires_at": now + self.cfg.invoice_expiry,
            "min_hold_seconds": self.cfg.min_hold,
        }

    def poll_invoices(self):
        """Reconcile entries; held invoices can finish as refunds."""
        now = int(self._clock())
        watched = ("AWAITING_PAYMENT", "HELD", "QUEUED", "REFUNDING")
        for d in self.store.by_state(*watched):
            if not self._is_held(d):
                continue
            status = self.backend.held_invoice_status(d["payment_hash"])
            c = d["commitment"]
            if status.state == "cancelled":
                target = "EXPIRED" if d["state"] == "AWAITING_PAYMENT" else "REFUNDED"
                self.store.transition(c, d["state"], target, entry_state="cancelled")
            elif status.state == "accepted":
                if d["state"] == "AWAITING_PAYMENT":
                    self.store.transition(
                        c, "AWAITING_PAYMENT", "HELD",
                        paid_at=status.accepted_at or now,
                        entry_state="accepted", entry_expiry=status.htlc_expiry,
                    )
                elif d["state"] in ("HELD", "QUEUED"):
                    self.store.update(
                        c, entry_state="accepted", entry_expiry=status.htlc_expiry,
                    )
            elif status.state == "paid":
                if d["state"] == "AWAITING_PAYMENT":
                    self.store.transition(
                        c, "AWAITING_PAYMENT", "HELD",
                        paid_at=status.accepted_at or now, entry_state="paid",
                        entry_expiry=status.htlc_expiry,
                    )
                elif d["state"] in ("HELD", "QUEUED"):
                    self.store.update(c, entry_state="paid")
            elif (d["state"] == "AWAITING_PAYMENT"
                  and now > d["created_at"] + self.cfg.invoice_expiry):
                self.backend.cancel_held_invoice(d["payment_hash"])
                self.store.transition(
                    c, "AWAITING_PAYMENT", "EXPIRED", entry_state="cancelled",
                )

        for d in self.store.by_state("AWAITING_PAYMENT"):
            if self._is_held(d):
                continue
            if now > d["created_at"] + self.cfg.invoice_expiry:
                self.store.transition(d["commitment"], "AWAITING_PAYMENT", "EXPIRED")
                continue
            paid, paid_at = self.backend.invoice_status(d["invoice_label"])
            if paid:
                self.store.transition(
                    d["commitment"], "AWAITING_PAYMENT", "HELD",
                    paid_at=paid_at or now,
                )

    # ── payout authorization ─────────────────────────────────
    def authorize_payout(self, commitment: str, root: str, nullifier_hash: str,
                         pubkey_hex: str, offer: str,
                         fee_bps: int, signature_hex: str) -> dict:
        self._require_enabled()
        d = self.store.get(commitment)
        if not d:
            raise ServiceError("unknown_commitment", "no such deposit", 404)
        if d["state"] not in ("HELD", "QUEUED"):
            raise ServiceError("not_payable", f"deposit is {d['state']}", 409)
        if d["state"] == "QUEUED":
            raise ServiceError("already_queued", "payout already authorized", 409)
        if not self.store.is_known_root(root):
            # only a root from the recent history is acceptable
            raise ServiceError("unknown_root",
                               "root is not in the recent root history", 409)
        if not offer or len(offer) > 1024 or any(c.isspace() for c in offer):
            raise ServiceError("bad_offer", "invalid BOLT12 offer", 422)
        if fee_bps != self.cfg.fee_bps:
            raise ServiceError("fee_mismatch",
                               f"current fee is {self.cfg.fee_bps} bps", 422)
        if not receipts.verify_authorization(
            commitment, root, nullifier_hash, pubkey_hex, signature_hex,
            offer, d["amount_sats"], fee_bps,
        ):
            raise ServiceError("bad_signature",
                               "authorization does not match root/nullifier/offer/fee", 401)

        oh = receipts.offer_hash(offer)
        jitter = self._randbelow(self.cfg.jitter_max + 1) if self.cfg.jitter_max else 0
        release_at = d["paid_at"] + self.cfg.min_hold + jitter
        if self._is_held(d):
            entry = self.backend.held_invoice_status(d["payment_hash"])
            if entry.state == "cancelled":
                self.store.transition(commitment, "HELD", "REFUNDED",
                                      entry_state="cancelled")
                raise ServiceError("entry_refunded",
                                   "the held entry has returned to the depositor", 409)
            if entry.state not in ("accepted", "paid"):
                raise ServiceError("not_payable",
                                   f"held entry is {entry.state}", 409)
            if (release_at + self.cfg.settlement_safety_seconds
                    >= d["created_at"] + self.cfg.invoice_expiry):
                raise ServiceError("entry_expiring",
                                   "not enough time remains for a safe exit", 409)
        payout = lattice.payout_amount(d["amount_sats"])
        result = self.store.queue_payout(
            commitment, offer, oh, nullifier_hash, release_at, fee_bps, payout,
        )
        if result == "offer_reuse":
            raise ServiceError("offer_reuse",
                               "every exit needs a fresh BOLT12 offer", 409)
        if result == "nullifier_spent":
            raise ServiceError("already_spent",
                               "this note's nullifier has already been spent", 409)
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
        """Recover a journaled exit without ever paying it a second time."""
        journal = self._journal_preimages()
        for d in self.store.by_state("SETTLING"):
            preimage = journal.get(d["commitment"])
            if not preimage:
                continue  # reconcile listpays manually; never blindly repay
            target = "EXIT_PAID" if self._is_held(d) else "SETTLED"
            if self.store.transition(
                d["commitment"], "SETTLING", target,
                preimage=preimage, offer=None,
            ):
                print(f"reconciled {d['commitment']} from settlement journal", flush=True)

    def settle_exit_paid_entries(self):
        """Claim held entries after their exit; safe to resume after a crash."""
        settled = []
        for d in self.store.by_state("EXIT_PAID", "ENTRY_REVIEW"):
            c = d["commitment"]
            try:
                entry = self.backend.held_invoice_status(d["payment_hash"])
                if entry.state != "paid" and entry.state != "cancelled":
                    self.backend.settle_held_invoice(d["entry_preimage"])
                    entry = self.backend.held_invoice_status(d["payment_hash"])
            except Exception:
                continue  # node hiccup; resume without repeating the exit
            if entry.state == "paid":
                if self.store.transition(
                    c, d["state"], "SETTLED", entry_state="paid",
                    entry_preimage=None, entry_settled_at=int(self._clock()),
                ):
                    settled.append(c)
            elif entry.state == "cancelled" and d["state"] != "ENTRY_REVIEW":
                # The user received the exit AND the entry refund. This is a
                # pool loss requiring operator review, never a fake SETTLED.
                self.store.transition(
                    c, d["state"], "ENTRY_REVIEW",
                    entry_state="cancelled", entry_preimage=None,
                )
        return settled

    def _held_exit_is_safe(self, d: dict, now: int) -> bool:
        """Cancel instead of starting an exit too close to entry expiry."""
        c = d["commitment"]
        entry = self.backend.held_invoice_status(d["payment_hash"])
        if entry.state == "cancelled":
            self.store.transition(c, "QUEUED", "REFUNDED",
                                  entry_state="cancelled", entry_preimage=None)
            return False
        if entry.state == "paid":
            return True
        expiry = entry.htlc_expiry or d["entry_expiry"]
        wall_deadline = d["created_at"] + self.cfg.invoice_expiry
        too_late = now + self.cfg.settlement_safety_seconds >= wall_deadline
        if expiry is not None:
            too_late = too_late or (
                self.backend.block_height() + self.cfg.settlement_safety_blocks
                >= expiry
            )
        if entry.state != "accepted" or too_late:
            self.backend.cancel_held_invoice(d["payment_hash"])
            self.store.transition(c, "QUEUED", "REFUNDING",
                                  entry_state="cancelled")
            return False
        return True

    def execute_due_payouts(self):
        """Pay due exits, then claim their held entries in a separate phase."""
        self.reconcile_settling()
        settled = self.settle_exit_paid_entries()
        now = int(self._clock())
        for d in self.store.by_state("QUEUED"):
            if d["release_at"] is None or now < d["release_at"]:
                continue
            if self._is_held(d):
                try:
                    if not self._held_exit_is_safe(d, now):
                        continue
                except BackendError:
                    continue  # no fresh node facts, no irreversible exit
            if not self.store.transition(d["commitment"], "QUEUED", "SETTLING"):
                continue
            try:
                if not d["offer"]:
                    raise BackendError("no offer available for payout")
                preimage = self.backend.pay_offer(d["offer"], d["payout_sats"])
            except PaymentAmbiguous:
                self.store.transition(d["commitment"], "SETTLING", "PAYOUT_REVIEW")
                continue
            except Exception:
                # Only backends with a definite failure may use this path.
                self.store.transition(d["commitment"], "SETTLING", "QUEUED")
                continue
            # Journal before the DB flip. Recovery reads this exact exit proof
            # and never pays the offer a second time.
            self._journal_append({
                "commitment": d["commitment"], "preimage": preimage,
                "amount_sats": d["payout_sats"], "at": now,
            })
            target = "EXIT_PAID" if self._is_held(d) else "SETTLED"
            self.store.transition(
                d["commitment"], "SETTLING", target,
                preimage=preimage, offer=None,
            )
            if target == "SETTLED":
                settled.append(d["commitment"])
        settled.extend(self.settle_exit_paid_entries())
        return list(dict.fromkeys(settled))

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
            "entry_mode": "held_htlc" if self._is_held(d) else "standard",
            "payment_hash": d["payment_hash"],
            "entry_state": d["entry_state"],
            "entry_expiry": d["entry_expiry"],
            "entry_settled_at": d["entry_settled_at"],
        }

    def anonymity_set(self) -> dict:
        # the set is ALL deposits ever — it never shrinks on settlement
        return {
            "size": self.tree.next_index,
            "merkle_root": self.tree.root.hex(),
            "root_history_size": merkle.ROOT_HISTORY_SIZE,
        }

    def inclusion_proof(self, commitment: str) -> dict:
        idx = self.store.leaf_index(commitment)
        if idx is None:
            raise ServiceError("not_in_set", "commitment is not in the tree", 404)
        prf = self.tree.path(idx)
        return {
            "leaf_index": idx,
            "merkle_root": self.tree.root.hex(),
            "proof": [{"sibling": s, "side": side} for s, side in prf],
        }
