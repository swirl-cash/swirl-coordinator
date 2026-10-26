"""Authenticated coordinator snapshots and deterministic shared Merkle rounds.

A federation round binds each coordinator's complete append-only local root.
The combined tree orders snapshots by coordinator id and preserves each local
leaf order, so every honest coordinator independently derives the same round.

This stage shares anonymity sets only. Exits and nullifiers remain at the
deposit's home coordinator; cross-coordinator exits are intentionally absent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from . import merkle

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_SNAPSHOT_LEAVES = 10_000
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class FederationError(RuntimeError):
    pass


@dataclass(frozen=True)
class FederationConfig:
    coordinator_id: str
    peers: tuple[str, ...]
    token: str
    allow_http: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.peers)


def from_env() -> FederationConfig | None:
    peers = tuple(
        p.strip().rstrip("/")
        for p in os.environ.get("SWIRL_FEDERATION_PEERS", "").split(",")
        if p.strip()
    )
    if not peers:
        return None
    cfg = FederationConfig(
        coordinator_id=os.environ.get("SWIRL_FEDERATION_ID", "local"),
        peers=peers,
        token=os.environ.get("SWIRL_FEDERATION_TOKEN", ""),
        allow_http=os.environ.get("SWIRL_FEDERATION_ALLOW_HTTP", "").lower()
        in ("1", "true", "yes", "on"),
    )
    if not ID_RE.fullmatch(cfg.coordinator_id):
        raise ValueError("invalid SWIRL_FEDERATION_ID")
    if not cfg.token:
        raise ValueError("SWIRL_FEDERATION_TOKEN is required with peers")
    for peer in peers:
        parsed = urlparse(peer)
        if parsed.scheme == "http" and not cfg.allow_http:
            raise ValueError("plaintext federation HTTP requires explicit opt-in")
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError(f"invalid federation peer: {peer}")
    return cfg


def fetch_snapshot(peer: str, cfg: FederationConfig) -> dict:
    req = urllib.request.Request(
        peer + "/v1/federation/snapshot",
        headers={"X-SWIRL-Federation-Token": cfg.token},
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            raw = response.read(MAX_SNAPSHOT_BYTES + 1)
    except Exception as exc:
        raise FederationError(f"snapshot fetch failed for {peer}: {exc}") from exc
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise FederationError("federation snapshot is too large")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FederationError("invalid federation snapshot JSON") from exc


def validate_snapshot(snapshot: dict, local_id: str) -> dict:
    if not isinstance(snapshot, dict):
        raise FederationError("snapshot must be an object")
    coordinator_id = snapshot.get("coordinator_id")
    commitments = snapshot.get("commitments")
    root = snapshot.get("merkle_root")
    if not isinstance(coordinator_id, str) or not ID_RE.fullmatch(coordinator_id):
        raise FederationError("invalid coordinator id")
    if coordinator_id == local_id:
        raise FederationError("peer returned the local coordinator id")
    if not isinstance(commitments, list) or len(commitments) > MAX_SNAPSHOT_LEAVES:
        raise FederationError("invalid snapshot leaf count")
    if not isinstance(root, str) or len(root) != 64:
        raise FederationError("invalid snapshot root")
    try:
        bytes.fromhex(root)
    except ValueError as exc:
        raise FederationError("invalid snapshot root hex") from exc

    tree = merkle.AppendOnlyTree()
    seen: set[str] = set()
    for commitment in commitments:
        if not isinstance(commitment, str) or len(commitment) != 64:
            raise FederationError("invalid commitment in snapshot")
        try:
            bytes.fromhex(commitment)
        except ValueError as exc:
            raise FederationError("invalid commitment hex") from exc
        if commitment in seen:
            raise FederationError("duplicate commitment in snapshot")
        seen.add(commitment)
        tree.insert(commitment)
    if tree.root.hex() != root:
        raise FederationError("snapshot root does not match its commitments")
    return {
        "coordinator_id": coordinator_id,
        "commitments": commitments,
        "merkle_root": root,
    }


def round_id(snapshots: list[dict]) -> str:
    digest = hashlib.sha256(b"SWIRL1/federation-round")
    for snapshot in sorted(snapshots, key=lambda s: s["coordinator_id"]):
        identifier = snapshot["coordinator_id"].encode()
        digest.update(len(identifier).to_bytes(2, "big"))
        digest.update(identifier)
        digest.update(bytes.fromhex(snapshot["merkle_root"]))
    return digest.hexdigest()
