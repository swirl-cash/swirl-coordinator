"""Append-only Merkle tree with root history — the Tornado model.

Two properties copied deliberately from MerkleTreeWithHistory.sol:

  * the tree is PERMANENT. Settled deposits are never removed, so the
    anonymity set only ever grows. (Rebuilding a tree of "currently held"
    deposits would shrink the crowd with every withdrawal — a design bug.)
  * the last ROOT_HISTORY_SIZE roots stay valid. A withdrawal may reference
    any recent root, so observers cannot correlate "root used" with
    "deposit made just before the withdrawal".

Hashing is SHA-256 (domain-separated) for now; the structure is exactly the
one a Groth16 membership proof will later be generated against.
"""

from __future__ import annotations

import hashlib

LEVELS = 20                      # same depth as tornado's Withdraw(20)
ROOT_HISTORY_SIZE = 30           # same as ROOT_HISTORY_SIZE in the contract


def _h(a: bytes, b: bytes) -> bytes:
    return hashlib.sha256(b"SWIRL1/node" + a + b).digest()


def leaf(commitment_hex: str) -> bytes:
    return hashlib.sha256(b"SWIRL1/leaf" + bytes.fromhex(commitment_hex)).digest()


def _zeros() -> list[bytes]:
    out = [hashlib.sha256(b"SWIRL1/zero").digest()]
    for _ in range(LEVELS):
        out.append(_h(out[-1], out[-1]))
    return out


class AppendOnlyTree:
    """Incremental tree, O(levels) per insert, replayable from leaf order.
    Leaves are retained so membership paths can be served directly."""

    def __init__(self):
        self.zeros = _zeros()
        self.filled: list[bytes] = list(self.zeros)
        self.leaves: list[bytes] = []
        self.next_index = 0
        self.root = self.zeros[LEVELS]

    def insert(self, commitment_hex: str) -> tuple[int, str]:
        """Returns (leaf_index, new_root_hex)."""
        if self.next_index >= 2 ** LEVELS:
            raise RuntimeError("merkle tree is full")
        index = self.next_index
        current = leaf(commitment_hex)
        self.leaves.append(current)
        i = index
        for level in range(LEVELS):
            if i % 2 == 0:
                left, right = current, self.zeros[level]
                self.filled[level] = current
            else:
                left, right = self.filled[level], current
            current = _h(left, right)
            i //= 2
        self.next_index += 1
        self.root = current
        return index, current.hex()

    def _node(self, level: int, index: int) -> bytes:
        start = index * (2 ** level)
        if start >= len(self.leaves):
            return self.zeros[level]
        if level == 0:
            return self.leaves[index]
        return _h(self._node(level - 1, index * 2),
                  self._node(level - 1, index * 2 + 1))

    def path(self, index: int) -> list[tuple[str, str]]:
        """Merkle path against the CURRENT root, [(sibling_hex, 'L'|'R')].
        'L' means the sibling sits on the left of the walking node."""
        if not 0 <= index < self.next_index:
            raise IndexError("leaf index out of range")
        out: list[tuple[str, str]] = []
        i = index
        for level in range(LEVELS):
            sibling = self._node(level, i ^ 1)
            out.append((sibling.hex(), "L" if i % 2 == 1 else "R"))
            i //= 2
        return out


def verify_path(commitment_hex: str, prf: list[tuple[str, str]], root_hex: str) -> bool:
    if len(prf) != LEVELS:
        return False
    node = leaf(commitment_hex)
    for sib_hex, side in prf:
        sib = bytes.fromhex(sib_hex)
        node = _h(sib, node) if side == "L" else _h(node, sib)
    return node.hex() == root_hex
