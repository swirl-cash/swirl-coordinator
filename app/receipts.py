"""SWIRL receipts and withdrawal authorization structure.

Receipt (generated and kept on the user's device):
    SWIRL1-<amount_sats>-<seckey hex>
From the secret key we derive:
    pubkey        = schnorr.pubkey(sk)                     (spending key)
    commitment    = sha256("SWIRL1/commitment" | pubkey | amount)
    nullifier     = sha256("SWIRL1/nullifier-seed" | sk)
    nullifierHash = sha256("SWIRL1/nullifier" | nullifier)

Double-spending is prevented by the NULLIFIER SET, not by the deposit state
machine: a withdrawal permanently burns nullifierHash.

The authorization binds root, nullifierHash, recipient and fee, plus the
direct mode's commitment:
    sha256("SWIRL1/withdraw" | commitment | root | nullifierHash |
           sha256(offer) | amount | fee_bps)
Changing the root, destination or fee invalidates the signature. When the
Groth16 path lands, the same tuple becomes the circuit's public input and
the commitment stops being revealed at all.
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


def nullifier_for(seckey: bytes) -> bytes:
    return hashlib.sha256(b"SWIRL1/nullifier-seed" + seckey).digest()


def nullifier_hash_for(seckey: bytes) -> str:
    return hashlib.sha256(b"SWIRL1/nullifier" + nullifier_for(seckey)).hexdigest()


def encode_receipt(seckey: bytes, amount_sats: int) -> str:
    return f"SWIRL1-{amount_sats}-{seckey.hex()}"


def decode_receipt(receipt: str) -> tuple[bytes, int]:
    m = RECEIPT_RE.match(receipt.strip())
    if not m:
        raise ValueError("malformed receipt — expected SWIRL1-<sats>-<64 hex>")
    return bytes.fromhex(m.group(2)), int(m.group(1))


def auth_digest(commitment: str, root: str, nullifier_hash: str,
                offer: str, amount_sats: int, fee_bps: int) -> bytes:
    return hashlib.sha256(
        b"SWIRL1/withdraw"
        + bytes.fromhex(commitment)
        + bytes.fromhex(root)
        + bytes.fromhex(nullifier_hash)
        + hashlib.sha256(offer.encode()).digest()
        + amount_sats.to_bytes(8, "big")
        + fee_bps.to_bytes(4, "big")
    ).digest()


def sign_authorization(receipt: str, commitment: str, root: str,
                       offer: str, fee_bps: int) -> tuple[str, str, str]:
    """User-side helper: returns (pubkey_hex, nullifier_hash_hex, signature_hex)."""
    sk, amount = decode_receipt(receipt)
    pk = schnorr.pubkey(sk)
    if make_commitment(pk, amount) != commitment:
        raise ValueError("receipt does not match this commitment")
    nh = nullifier_hash_for(sk)
    sig = schnorr.sign(auth_digest(commitment, root, nh, offer, amount, fee_bps), sk)
    return pk.hex(), nh, sig.hex()


def verify_authorization(commitment: str, root: str, nullifier_hash: str,
                         pub_hex: str, sig_hex: str,
                         offer: str, amount_sats: int, fee_bps: int) -> bool:
    try:
        pub = bytes.fromhex(pub_hex)
        sig = bytes.fromhex(sig_hex)
    except ValueError:
        return False
    if make_commitment(pub, amount_sats) != commitment:
        return False
    return schnorr.verify(
        auth_digest(commitment, root, nullifier_hash, offer, amount_sats, fee_bps),
        pub, sig,
    )


def offer_hash(offer: str) -> str:
    return hashlib.sha256(offer.strip().encode()).hexdigest()
