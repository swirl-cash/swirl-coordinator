"""FastAPI surface. All protocol rules live in service.py — this layer only
translates HTTP. No request logging of receipts or offers (run uvicorn with
access logs off, or behind a reverse proxy that does not log bodies).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .lightning import from_env
from .service import Coordinator, Config, ServiceError
from .store import Store


class DepositReq(BaseModel):
    amount_sats: int = Field(..., gt=0)
    pubkey: str


class PayoutReq(BaseModel):
    commitment: str
    root: str
    nullifier_hash: str
    pubkey: str
    offer: str = Field(..., min_length=4, max_length=1024)
    fee_bps: int
    signature: str


def build_app() -> FastAPI:
    cfg = Config(
        min_hold=int(os.environ.get("SWIRL_MIN_HOLD", "3600")),
        jitter_max=int(os.environ.get("SWIRL_JITTER_MAX", "900")),
        invoice_expiry=int(os.environ.get("SWIRL_INVOICE_EXPIRY", "3600")),
    )
    coord = Coordinator(
        Store(os.environ.get("SWIRL_DB", "data/swirl.db")), from_env(), cfg,
    )
    app = FastAPI(title="SWIRL coordinator", version=__version__)
    app.state.coord = coord

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, e: ServiceError):
        return JSONResponse(status_code=e.http,
                            content={"error": e.code, "message": e.message})

    # ── edge hardening: body cap + per-IP sliding-window rate limit ──
    # Single-process in-memory limiter (the API is one process by design).
    # Behind a reverse proxy, also enforce limits there and pass a trusted
    # client-IP header; do not trust X-Forwarded-For blindly here.
    import threading
    import time as _time
    from collections import defaultdict, deque

    _hits: dict[str, deque] = defaultdict(deque)
    _hits_lock = threading.Lock()

    @app.middleware("http")
    async def edge_guard(request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            cl = request.headers.get("content-length")
            if cl and cl.isdigit() and int(cl) > 32 * 1024:
                return JSONResponse(status_code=413,
                                    content={"error": "body_too_large"})
            bucket, limit = "w", 30      # 30 writes/min/IP
        else:
            bucket, limit = "r", 120     # 120 reads/min/IP
        ip = request.client.host if request.client else "unknown"
        key = f"{bucket}:{ip}"
        now = _time.monotonic()
        with _hits_lock:
            q = _hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                return JSONResponse(status_code=429,
                                    content={"error": "rate_limited"})
            q.append(now)
        return await call_next(request)

    @app.get("/v1/params")
    def params():
        return coord.params()

    @app.get("/v1/health")
    def health():
        return {"ok": True}

    @app.post("/v1/mix/deposit", status_code=201)
    def deposit(req: DepositReq):
        return coord.create_deposit(req.amount_sats, req.pubkey)

    @app.post("/v1/mix/payout")
    def payout(req: PayoutReq):
        return coord.authorize_payout(
            req.commitment, req.root, req.nullifier_hash,
            req.pubkey, req.offer, req.fee_bps, req.signature,
        )

    @app.get("/v1/mix/deposit/{commitment}")
    def status(commitment: str):
        return coord.status(commitment)

    @app.get("/v1/mix/set")
    def anonymity_set():
        return coord.anonymity_set()

    @app.get("/v1/mix/proof/{commitment}")
    def proof(commitment: str):
        return coord.inclusion_proof(commitment)

    return app


app = build_app()
