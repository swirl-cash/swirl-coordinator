"""SQLite persistence. One file, WAL mode, stdlib only.

Privacy rule: the DB never stores receipt secret keys, only commitments.
Exit offers are kept only while a payout is queued (the node must know the
destination to pay it) and are purged the moment the payout settles; the
single-use offer hash is retained forever to block reuse.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS deposits (
    commitment   TEXT PRIMARY KEY,
    amount_sats  INTEGER NOT NULL,
    pubkey       TEXT NOT NULL,
    state        TEXT NOT NULL,           -- AWAITING_PAYMENT/HELD/QUEUED/SETTLED/EXPIRED
    invoice      TEXT,
    invoice_label TEXT,
    created_at   INTEGER NOT NULL,
    paid_at      INTEGER,
    release_at   INTEGER,
    offer        TEXT,               -- transient, purged on settlement
    offer_hash   TEXT,
    fee_bps      INTEGER,
    payout_sats  INTEGER,
    preimage     TEXT,              -- exit payment proof
    payment_hash TEXT,              -- held entry; NULL for legacy standard entries
    entry_preimage TEXT,            -- claim secret, purged after entry settlement
    entry_state  TEXT,              -- unpaid/accepted/paid/cancelled
    entry_expiry INTEGER,           -- shortest accepted HTLC CLTV expiry (blocks)
    entry_settled_at INTEGER
);
CREATE TABLE IF NOT EXISTS used_offers (
    offer_hash TEXT PRIMARY KEY,
    used_at    INTEGER NOT NULL
);
-- permanent deposit leaves: settled deposits are NEVER removed, so the
-- anonymity set only grows
CREATE TABLE IF NOT EXISTS leaves (
    commitment  TEXT PRIMARY KEY,
    leaf_index  INTEGER UNIQUE NOT NULL,
    amount_sats INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);
-- rolling history of recent roots; the last 30 are valid for withdrawals
CREATE TABLE IF NOT EXISTS roots (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    root TEXT NOT NULL
);
-- spent nullifiers: double-spend prevention independent of deposit state
CREATE TABLE IF NOT EXISTS nullifiers (
    nullifier_hash TEXT PRIMARY KEY,
    spent_at       INTEGER NOT NULL
);
-- authenticated local/peer snapshots used to derive shared Merkle rounds
CREATE TABLE IF NOT EXISTS federation_snapshots (
    coordinator_id TEXT PRIMARY KEY,
    root           TEXT NOT NULL,
    commitments    TEXT NOT NULL,
    synced_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS federation_rounds (
    round_id   TEXT PRIMARY KEY,
    root       TEXT UNIQUE NOT NULL,
    created_at INTEGER NOT NULL
);
"""

# states in which a commitment is part of the live anonymity set
LIVE_STATES = ("HELD", "QUEUED", "SETTLING")


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(SCHEMA)
            # SQLite does not add new columns through CREATE TABLE IF NOT
            # EXISTS; migrate existing coordinator databases in place.
            columns = {
                row[1] for row in self._conn.execute(
                    "PRAGMA table_info(deposits)"
                ).fetchall()
            }
            migrations = {
                "payment_hash": "TEXT",
                "entry_preimage": "TEXT",
                "entry_state": "TEXT",
                "entry_expiry": "INTEGER",
                "entry_settled_at": "INTEGER",
            }
            for name, sql_type in migrations.items():
                if name not in columns:
                    self._conn.execute(
                        f"ALTER TABLE deposits ADD COLUMN {name} {sql_type}"
                    )
            self._conn.commit()

    def close(self):
        self._conn.close()

    def _q(self, sql, params=(), fetch="all"):
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            self._conn.commit()
        if fetch == "one":
            return dict(rows[0]) if rows else None
        return [dict(r) for r in rows]

    # ── deposits ─────────────────────────────────────────────
    def insert_deposit(self, d: dict):
        fields = (
            "commitment", "amount_sats", "pubkey", "state", "invoice",
            "invoice_label", "created_at", "payment_hash", "entry_preimage",
            "entry_state",
        )
        row = {name: d.get(name) for name in fields}
        self._q(
            f"""INSERT INTO deposits ({', '.join(fields)})
               VALUES ({', '.join(':' + name for name in fields)})""",
            row,
        )

    def get(self, commitment: str) -> dict | None:
        return self._q("SELECT * FROM deposits WHERE commitment=?", (commitment,), "one")

    def by_state(self, *states: str) -> list[dict]:
        marks = ",".join("?" for _ in states)
        return self._q(f"SELECT * FROM deposits WHERE state IN ({marks})", states)

    def update(self, commitment: str, **fields):
        fields = {k: (int(v) if isinstance(v, float) else v) for k, v in fields.items()}
        sets = ", ".join(f"{k}=?" for k in fields)
        self._q(
            f"UPDATE deposits SET {sets} WHERE commitment=?",
            (*fields.values(), commitment),
        )

    def transition(self, commitment: str, from_state: str, to_state: str, **fields) -> bool:
        """Atomic compare-and-set; prevents double-settlement races."""
        fields = {k: (int(v) if isinstance(v, float) else v) for k, v in fields.items()}
        sets = ("state=?" + (", " + ", ".join(f"{k}=?" for k in fields) if fields else ""))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE deposits SET {sets} WHERE commitment=? AND state=?",
                (to_state, *fields.values(), commitment, from_state),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def live_commitments(self) -> list[str]:
        marks = ",".join("?" for _ in LIVE_STATES)
        return [
            r["commitment"]
            for r in self._q(f"SELECT commitment FROM deposits WHERE state IN ({marks})", LIVE_STATES)
        ]

    # ── permanent merkle leaves + root history ───────────────
    def add_leaf(self, commitment: str, amount_sats: int,
                 leaf_index: int, root: str):
        with self._lock:
            self._conn.execute(
                "INSERT INTO leaves (commitment, leaf_index, amount_sats, created_at)"
                " VALUES (?, ?, ?, ?)",
                (commitment, leaf_index, amount_sats, int(time.time())),
            )
            self._conn.execute("INSERT INTO roots (root) VALUES (?)", (root,))
            self._conn.commit()

    def leaves_in_order(self) -> list[str]:
        return [r["commitment"] for r in
                self._q("SELECT commitment FROM leaves ORDER BY leaf_index")]

    def leaf_index(self, commitment: str) -> int | None:
        r = self._q("SELECT leaf_index FROM leaves WHERE commitment=?",
                    (commitment,), "one")
        return r["leaf_index"] if r else None

    def is_known_root(self, root: str) -> bool:
        return self._q(
            "SELECT 1 AS x FROM (SELECT root FROM roots ORDER BY id DESC LIMIT 30)"
            " WHERE root=?", (root,), "one") is not None

    # ── authenticated federation snapshots + shared roots ────
    def upsert_federation_snapshot(self, coordinator_id: str, root: str,
                                   commitments: list[str]):
        import json
        self._q(
            """INSERT INTO federation_snapshots
               (coordinator_id, root, commitments, synced_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(coordinator_id) DO UPDATE SET
               root=excluded.root, commitments=excluded.commitments,
               synced_at=excluded.synced_at""",
            (coordinator_id, root, json.dumps(commitments, separators=(",", ":")),
             int(time.time())),
        )

    def federation_snapshots(self) -> list[dict]:
        import json
        rows = self._q(
            "SELECT coordinator_id, root, commitments FROM federation_snapshots"
            " ORDER BY coordinator_id"
        )
        return [{
            "coordinator_id": row["coordinator_id"],
            "merkle_root": row["root"],
            "commitments": json.loads(row["commitments"]),
        } for row in rows]

    def record_federation_root(self, round_id: str, root: str):
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO federation_rounds (round_id, root, created_at)"
                " VALUES (?, ?, ?)", (round_id, root, int(time.time())),
            )
            if cur.rowcount == 1:
                self._conn.execute("INSERT INTO roots (root) VALUES (?)", (root,))
            self._conn.commit()

    # ── payout queueing (single atomic transaction) ─────────
    def queue_payout(self, commitment: str, offer: str, offer_hash: str,
                     nullifier_hash: str,
                     release_at: int, fee_bps: int, payout_sats: int) -> str:
        """Burn the single-use offer AND the nullifier AND move HELD→QUEUED
        atomically. Returns 'ok' | 'offer_reuse' | 'nullifier_spent' | 'state'.
        If anything fails, NOTHING is burned and the deposit stays HELD.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if self._conn.execute(
                    "SELECT 1 FROM used_offers WHERE offer_hash=?", (offer_hash,)
                ).fetchone():
                    self._conn.execute("ROLLBACK")
                    return "offer_reuse"
                if self._conn.execute(
                    "SELECT 1 FROM nullifiers WHERE nullifier_hash=?", (nullifier_hash,)
                ).fetchone():
                    self._conn.execute("ROLLBACK")
                    return "nullifier_spent"
                cur = self._conn.execute(
                    """UPDATE deposits
                       SET state='QUEUED', release_at=?, offer=?, offer_hash=?,
                           fee_bps=?, payout_sats=?
                       WHERE commitment=? AND state='HELD'""",
                    (release_at, offer, offer_hash, fee_bps, payout_sats, commitment),
                )
                if cur.rowcount != 1:
                    self._conn.execute("ROLLBACK")
                    return "state"
                self._conn.execute(
                    "INSERT INTO used_offers (offer_hash, used_at) VALUES (?, ?)",
                    (offer_hash, int(time.time())),
                )
                self._conn.execute(
                    "INSERT INTO nullifiers (nullifier_hash, spent_at) VALUES (?, ?)",
                    (nullifier_hash, int(time.time())),
                )
                self._conn.execute("COMMIT")
                return "ok"
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
