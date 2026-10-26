"""BIP340 Schnorr signatures over secp256k1 — pure stdlib, no dependencies.

Used for payout authorizations: the receipt secret key never leaves the
user's device; the coordinator only ever sees the public key and a signature
binding (commitment, offer, amount, fee) together.

Reference: https://github.com/bitcoin/bips/blob/master/bip-0340.mediawiki
"""

from __future__ import annotations

import hashlib
import secrets

# secp256k1 field / group parameters
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


def _sha(*chunks: bytes) -> bytes:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.digest()


def _tagged(tag: str, msg: bytes) -> bytes:
    th = _sha(tag.encode())
    return _sha(th, th, msg)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    x1, y1 = a
    x2, y2 = b
    if x1 == x2 and (y1 != y2 or y1 == 0):
        return None
    if a == b:
        lam = (3 * x1 * x1) * pow(2 * y1, P - 2, P) % P
    else:
        lam = (y2 - y1) * pow(x2 - x1, P - 2, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def _mul(k: int, point=G):
    r = None
    a = point
    while k:
        if k & 1:
            r = _add(r, a)
        a = _add(a, a)
        k >>= 1
    return r


def _lift_x(x: int):
    if not 0 <= x < P:
        raise ValueError("x out of range")
    y_sq = (pow(x, 3, P) + 7) % P
    y = pow(y_sq, (P + 1) // 4, P)
    if pow(y, 2, P) != y_sq:
        raise ValueError("not a curve point")
    return (x, y if y % 2 == 0 else P - y)


def pubkey(seckey: bytes) -> bytes:
    """x-only public key, 32 bytes."""
    d = int.from_bytes(seckey, "big")
    if not 1 <= d < N:
        raise ValueError("secret key out of range")
    return _mul(d)[0].to_bytes(32, "big")


def sign(msg32: bytes, seckey: bytes, aux_rand: bytes | None = None) -> bytes:
    """BIP340 signature of a 32-byte message. Deterministic if aux is fixed."""
    if len(msg32) != 32:
        raise ValueError("message must be 32 bytes")
    d0 = int.from_bytes(seckey, "big")
    if not 1 <= d0 < N:
        raise ValueError("secret key out of range")
    aux = secrets.token_bytes(32) if aux_rand is None else aux_rand
    pt = _mul(d0)
    d = d0 if pt[1] % 2 == 0 else N - d0
    pk = pt[0].to_bytes(32, "big")
    t = d ^ int.from_bytes(_tagged("BIP0340/aux", aux), "big")
    rand = _tagged("BIP0340/nonce", t.to_bytes(32, "big") + pk + msg32)
    k0 = int.from_bytes(rand, "big") % N
    if k0 == 0:
        raise RuntimeError("nonce failure")
    rt = _mul(k0)
    k = k0 if rt[1] % 2 == 0 else N - k0
    r = rt[0].to_bytes(32, "big")
    e = int.from_bytes(_tagged("BIP0340/challenge", r + pk + msg32), "big") % N
    sig = r + ((k + e * d) % N).to_bytes(32, "big")
    if not verify(msg32, pk, sig):
        raise RuntimeError("self-verification failed")
    return sig


def verify(msg32: bytes, pub: bytes, sig: bytes) -> bool:
    if len(msg32) != 32 or len(pub) != 32 or len(sig) != 64:
        return False
    try:
        pt = _lift_x(int.from_bytes(pub, "big"))
    except ValueError:
        return False
    r = int.from_bytes(sig[:32], "big")
    s = int.from_bytes(sig[32:], "big")
    if r >= P or s >= N:
        return False
    e = int.from_bytes(_tagged("BIP0340/challenge", sig[:32] + pub + msg32), "big") % N
    rt = _add(_mul(s), _mul(N - e, pt))
    if rt is None or rt[1] % 2 != 0 or rt[0] != r:
        return False
    return True
