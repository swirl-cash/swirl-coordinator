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
    preimage     TEXT
);
CREATE TABLE IF NOT EXISTS used_offers (
    offer_hash TEXT PRIMARY KEY,
    used_at    INTEGER NOT NULL
);
"""

# states in which a commitment is part of the live anonymity set
LIVE_STATES = ("HELD", "QUEUED", "SETTLING")


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)

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
        self._q(
            """INSERT INTO deposits
               (commitment, amount_sats, pubkey, state, invoice, invoice_label, created_at)
               VALUES (:commitment, :amount_sats, :pubkey, :state, :invoice,
                       :invoice_label, :created_at)""",
            d,
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

    # ── single-use offers ────────────────────────────────────
    def claim_offer(self, offer_hash: str) -> bool:
        """Returns False if this offer was ever used before."""
        try:
            self._q(
                "INSERT INTO used_offers (offer_hash, used_at) VALUES (?, ?)",
                (offer_hash, int(time.time())),
            )
            return True
        except sqlite3.IntegrityError:
            return False
