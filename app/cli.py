"""SWIRL user CLI — receipts are created and signed on YOUR machine.

    python -m app.cli keygen --amount 1000000
        -> prints your receipt (KEEP IT SECRET) and the pubkey to deposit with
    python -m app.cli deposit --receipt SWIRL1-... --api http://localhost:8080
    python -m app.cli sign --receipt SWIRL1-... --commitment <hex> --offer lno1...
        -> prints pubkey + signature for POST /v1/mix/payout
    python -m app.cli payout --receipt ... --commitment <hex> --offer lno1... --api ...
    python -m app.cli status --commitment <hex> --api ...

The receipt secret key is never sent to any server — only the pubkey and a
Schnorr signature over (commitment, offer, amount, fee).
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import urllib.request

from . import lattice, receipts, schnorr


def _api(api: str, path: str, payload: dict | None = None):
    url = api.rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(e.read().decode(), file=sys.stderr)
        sys.exit(1)


def _fee_bps(api: str) -> int:
    return _api(api, "/v1/params")["fee_bps"]


def main():
    p = argparse.ArgumentParser(prog="swirl", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    kg = sub.add_parser("keygen", help="create a fresh receipt for one quantum")
    kg.add_argument("--amount", type=int, required=True,
                    help=f"sats, one of {list(lattice.LATTICE)}")

    for name in ("deposit", "payout"):
        c = sub.add_parser(name)
        c.add_argument("--receipt", required=True)
        c.add_argument("--api", default="http://localhost:8080")
        if name == "payout":
            c.add_argument("--commitment", required=True)
            c.add_argument("--offer", required=True, help="fresh BOLT12 offer")
            c.add_argument("--root", help="recent merkle root (auto-fetched if omitted)")

    sg = sub.add_parser("sign", help="sign a payout authorization offline")
    sg.add_argument("--receipt", required=True)
    sg.add_argument("--commitment", required=True)
    sg.add_argument("--root", required=True)
    sg.add_argument("--offer", required=True)
    sg.add_argument("--fee-bps", type=int, default=lattice.FEE_BPS)

    st = sub.add_parser("status")
    st.add_argument("--commitment", required=True)
    st.add_argument("--api", default="http://localhost:8080")

    a = p.parse_args()

    if a.cmd == "keygen":
        lattice.check_quantum(a.amount)
        sk = secrets.token_bytes(32)
        pk = schnorr.pubkey(sk)
        commitment = receipts.make_commitment(pk, a.amount)
        print(json.dumps({
            "receipt": receipts.encode_receipt(sk, a.amount),
            "pubkey": pk.hex(),
            "commitment": commitment,
            "warning": "the receipt is the only key to these funds — back it up, never share it",
        }, indent=2))

    elif a.cmd == "deposit":
        sk, amount = receipts.decode_receipt(a.receipt)
        res = _api(a.api, "/v1/mix/deposit",
                   {"amount_sats": amount, "pubkey": schnorr.pubkey(sk).hex()})
        print(json.dumps(res, indent=2))

    elif a.cmd == "sign":
        pk, nh, sig = receipts.sign_authorization(
            a.receipt, a.commitment, a.root, a.offer, a.fee_bps)
        print(json.dumps({"pubkey": pk, "nullifier_hash": nh, "signature": sig}, indent=2))

    elif a.cmd == "payout":
        fee = _fee_bps(a.api)
        root = a.root or _api(a.api, "/v1/mix/set")["merkle_root"]
        pk, nh, sig = receipts.sign_authorization(
            a.receipt, a.commitment, root, a.offer, fee)
        res = _api(a.api, "/v1/mix/payout", {
            "commitment": a.commitment, "root": root, "nullifier_hash": nh,
            "pubkey": pk, "offer": a.offer, "fee_bps": fee, "signature": sig,
        })
        print(json.dumps(res, indent=2))

    elif a.cmd == "status":
        print(json.dumps(_api(a.api, f"/v1/mix/deposit/{a.commitment}"), indent=2))


if __name__ == "__main__":
    main()
