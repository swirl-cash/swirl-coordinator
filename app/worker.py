"""Background worker: poll invoices and settle due payouts.

Run alongside the API:
    python -m app.worker
Shares the same SQLite file (WAL) and env config as app.main.
"""

from __future__ import annotations

import os
import time

from . import federation
from .lightning import from_env
from .service import Coordinator, Config
from .store import Store


def main(interval: float = 5.0):
    cfg = Config(
        min_hold=int(os.environ.get("SWIRL_MIN_HOLD", "3600")),
        jitter_max=int(os.environ.get("SWIRL_JITTER_MAX", "900")),
        invoice_expiry=int(os.environ.get("SWIRL_INVOICE_EXPIRY", "3600")),
        settlement_safety_seconds=int(
            os.environ.get("SWIRL_SETTLEMENT_SAFETY_SECONDS", "900")
        ),
        settlement_safety_blocks=int(
            os.environ.get("SWIRL_SETTLEMENT_SAFETY_BLOCKS", "12")
        ),
    )
    coord = Coordinator(
        Store(os.environ.get("SWIRL_DB", "data/swirl.db")), from_env(), cfg,
        federation_cfg=federation.from_env(),
    )
    while True:
        try:
            for error in coord.sync_federation():
                print(f"federation sync failed: {error}", flush=True)
            for error in coord.commit_federation_nullifiers():
                print(f"federation commit failed: {error}", flush=True)
            coord.poll_invoices()
            settled = coord.execute_due_payouts()
            for c in settled:
                print(f"settled {c}", flush=True)
        except Exception as e:  # the worker must never die on a node hiccup
            print(f"worker round failed: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
