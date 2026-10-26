"""FastAPI surface. All protocol rules live in service.py — this layer only
translates HTTP. No request logging of receipts or offers (run uvicorn with
access logs off, or behind a reverse proxy that does not log bodies).
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .lightning import from_env
from .service import Coordinator, Config, ServiceError
from .store import Store


class DepositReq(BaseModel):
    amount_sats: int = Field(..., gt=0)
    pubkey: str


class PayoutReq(BaseModel):
    commitment: str
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
    app = FastAPI(title="SWIRL coordinator", version="0.1.0")
    app.state.coord = coord

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, e: ServiceError):
        return JSONResponse(status_code=e.http,
                            content={"error": e.code, "message": e.message})

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
            req.commitment, req.pubkey, req.offer, req.fee_bps, req.signature,
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
