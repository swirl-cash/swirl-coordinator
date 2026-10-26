"""Receipts and payout authorizations.

Deposit side (on the user's device, e.g. CLI or web wallet):
    sk = secrets.token_bytes(32)
    pk = schnorr.pubkey(sk)
    commitment = make_commitment(pk, amount_sats)
The user sends only (amount, pk) to the coordinator and keeps a receipt:
    SWIRL1-<amount_sats>-<seckey hex>

Payout side: the user signs an authorization binding the exact payout terms
    sha256("SWIRL1/payout" | commitment | sha256(offer) | amount | fee_bps)
and sends (pubkey, signature, offer, fee_bps). The secret key itself is never
transmitted. The coordinator cannot raise the fee or swap the destination
offer without invalidating the signature.
"""

from __future__ import annotations

import hashlib
import re

from . import schnorr

RECEIPT_RE = re.compile(r"^SWIRL1-(\d+)-([0-9a-f]{64})$")


def make_commitment(pub: bytes, amount_sats: int) -> str:
    return hashlib.sha256(
        b"SWIRL1/commitment" + pub + amount_sats.to_bytes(8, "big")
    ).hexdigest()


def encode_receipt(seckey: bytes, amount_sats: int) -> str:
    return f"SWIRL1-{amount_sats}-{seckey.hex()}"


def decode_receipt(receipt: str) -> tuple[bytes, int]:
    m = RECEIPT_RE.match(receipt.strip())
    if not m:
        raise ValueError("malformed receipt — expected SWIRL1-<sats>-<64 hex>")
    return bytes.fromhex(m.group(2)), int(m.group(1))


def auth_digest(commitment: str, offer: str, amount_sats: int, fee_bps: int) -> bytes:
    return hashlib.sha256(
        b"SWIRL1/payout"
        + bytes.fromhex(commitment)
        + hashlib.sha256(offer.encode()).digest()
        + amount_sats.to_bytes(8, "big")
        + fee_bps.to_bytes(4, "big")
    ).digest()


def sign_authorization(
    receipt: str, commitment: str, offer: str, fee_bps: int
) -> tuple[str, str]:
    """User-side helper: returns (pubkey_hex, signature_hex)."""
    sk, amount = decode_receipt(receipt)
    pk = schnorr.pubkey(sk)
    if make_commitment(pk, amount) != commitment:
        raise ValueError("receipt does not match this commitment")
    sig = schnorr.sign(auth_digest(commitment, offer, amount, fee_bps), sk)
    return pk.hex(), sig.hex()


def verify_authorization(
    commitment: str, pub_hex: str, sig_hex: str,
    offer: str, amount_sats: int, fee_bps: int,
) -> bool:
    try:
        pub = bytes.fromhex(pub_hex)
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    if make_commitment(pub, amount_sats) != commitment:
        return False
    return schnorr.verify(
        auth_digest(commitment, offer, amount_sats, fee_bps), pub, sig
    )


def offer_hash(offer: str) -> str:
    return hashlib.sha256(offer.strip().encode()).hexdigest()
