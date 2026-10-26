"""Merkle tree over held commitments.

Every deposit that is currently inside the pool is a leaf. Publishing the
root lets anyone verify that a commitment is (or is not) part of the live
anonymity set — and is the hook federated coordinators will later merge
their trees through.
"""

from __future__ import annotations

import hashlib


def _h(a: bytes, b: bytes) -> bytes:
    return hashlib.sha256(a + b).digest()


def leaf(commitment_hex: str) -> bytes:
    return hashlib.sha256(b"SWIRL1/leaf" + bytes.fromhex(commitment_hex)).digest()


def build(commitments: list[str]) -> tuple[bytes, list[list[bytes]]]:
    """Returns (root, levels). Empty set commits to sha256 of a fixed tag."""
    if not commitments:
        return hashlib.sha256(b"SWIRL1/empty").digest(), []
    level = [leaf(c) for c in sorted(commitments)]
    levels = [level]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            a = level[i]
            b = level[i + 1] if i + 1 < len(level) else a
            nxt.append(_h(a, b))
        levels.append(nxt)
        level = nxt
    return levels[-1][0], levels


def proof(commitments: list[str], commitment_hex: str) -> list[tuple[str, str]] | None:
    """Merkle inclusion proof as [(sibling_hex, 'L'|'R'), ...]."""
    if commitment_hex not in commitments:
        return None
    ordered = sorted(commitments)
    idx = ordered.index(commitment_hex)
    _, levels = build(commitments)
    out: list[tuple[str, str]] = []
    for level in levels[:-1]:
        sib = idx ^ 1
        if sib < len(level):
            sibling = level[sib]
            side = "L" if idx % 2 == 1 else "R"
        else:
            sibling = level[idx]
            side = "R"
        out.append((sibling.hex(), side))
        idx //= 2
    return out


def verify_proof(commitment_hex: str, prf: list[tuple[str, str]], root_hex: str) -> bool:
    node = leaf(commitment_hex)
    for sib_hex, side in prf:
        sib = bytes.fromhex(sib_hex)
        node = _h(sib, node) if side == "L" else _h(node, sib)
    return node.hex() == root_hex
