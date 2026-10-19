# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A stand-in for Celerp's relay and its object storage, for the integration validation
suite: two installations, each its own server process with its own database, reach it
over HTTP exactly as they reach the real relay.

It keeps only what the company copy link and the company key connection exchange with
the relay: the gateway hello, relay sign-in, a company copy's record, its encrypted file
and its email (kept in an outbox the suite reads), and a connector's stored key. A
request for an installation's public address (RELAY_PUBLIC_HOSTS maps each gateway key
to its host) is carried to it over its gateway connection, as the relay does. Run it
with ``python tests/integration_relay.py <port>``."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

# The object store host the relay hands out; the suite routes it to this process.
STORAGE = "https://celerp-handoffs.sgp1.digitaloceanspaces.com"

app = FastAPI()
_copies: dict[str, dict] = {}       # copy id -> record
_files: dict[str, bytes] = {}       # copy id hex -> ciphertext
_outbox: list[dict] = []            # every company copy email, in sending order
_tokens: dict[tuple[str, str], dict] = {}  # (instance id, connector) -> stored key
_sessions: dict[str, str] = {}      # session token -> instance id
_public: dict[str, str] = json.loads(os.environ.get("RELAY_PUBLIC_HOSTS", "{}"))  # gateway key -> host
_tunnels: dict[str, WebSocket] = {}  # public host -> its installation's live connection
_waiting: dict[str, asyncio.Future] = {}  # carried request id -> its response


@app.middleware("http")
async def tunnel(request: Request, call_next):
    """Carry a request for an installation's public address over its gateway connection."""
    host = request.headers.get("host", "").split(":")[0]
    if host not in _public.values():
        return await call_next(request)
    ws = _tunnels.get(host)
    if ws is None:
        return Response("The installation is not connected", status_code=502)
    request_id = uuid.uuid4().hex
    _waiting[request_id] = asyncio.get_running_loop().create_future()
    try:
        await ws.send_json({"type": "http.request", "payload": {
            "id": request_id, "method": request.method, "path": request.url.path,
            "query": request.url.query, "headers": dict(request.headers),
            "body_b64": base64.b64encode(await request.body()).decode()}})
        answer = await asyncio.wait_for(_waiting[request_id], 120)
    finally:
        _waiting.pop(request_id, None)
    response = Response(base64.b64decode(answer.get("body_b64") or ""), status_code=answer["status"])
    for name, value in answer.get("headers") or []:
        if name.lower() not in ("content-length", "transfer-encoding", "connection"):
            response.headers.append(name, value)
    return response


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(copy: dict) -> dict:
    return {k: copy[k] for k in ("id", "link_id", "operation_id", "company_id", "firm_name",
                                 "status", "sent_at", "expires_at")} | {
        "stored": copy["id"].replace("-", "") in _files, "can_restore": False,
        "opened": copy["fetched"], "downloaded": copy["fetched"], "company_opened": False}


def _by_link(link_id: str, proof: str) -> dict:
    for copy in _copies.values():
        if copy["link_id"] == link_id and secrets.compare_digest(copy["proof_hash"], str(proof)):
            return copy
    raise HTTPException(404, "Company copy not found")


@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Sign-in and the gateway session ─────────────────────────────────────────

@app.post("/auth/token")
async def auth_token(request: Request):
    body = await request.json()
    if not body.get("api_key"):
        raise HTTPException(401, "Unknown key")
    return {"access_token": "relay-" + hashlib.sha256(body["api_key"].encode()).hexdigest()[:24]}


@app.post("/auth/checkin")
async def checkin():
    return {}


@app.websocket("/ws/connect")
async def connect(ws: WebSocket):
    await ws.accept()
    host = None
    try:
        hello = await ws.receive_json()
        payload = hello.get("payload", {})
        iid = payload.get("instance_id") or str(uuid.uuid4())
        session = "session-" + secrets.token_hex(12)
        _sessions[session] = iid
        host = _public.get(payload.get("gateway_token", ""))
        if host:
            _tunnels[host] = ws
        await ws.send_json({"type": "hello_ack", "id": hello.get("id"),
                            "payload": {"instance_id": iid, "session_token": session}})
        while True:
            frame = await ws.receive_json()
            answer = frame.get("payload", {})
            if frame.get("type") == "http.response" and answer.get("id") in _waiting:
                _waiting[answer["id"]].set_result(answer)
    except WebSocketDisconnect:
        return
    finally:
        if host and _tunnels.get(host) is ws:
            del _tunnels[host]


def _instance(token: str | None) -> str:
    """The installation a live gateway session belongs to; its keys outlast a reconnect."""
    if not token or token not in _sessions:
        raise HTTPException(401, "No relay session")
    return _sessions[token]


# ── Company copies ───────────────────────────────────────────────────────────

@app.get("/handoffs")
async def list_copies(company_id: str, operation_id: str | None = None):
    rows = [c for c in _copies.values() if c["company_id"] == company_id
            and (operation_id is None or c["operation_id"] == operation_id)]
    return {"items": [_row(c) for c in sorted(rows, key=lambda c: c["created_at"], reverse=True)]}


@app.post("/handoffs", status_code=201)
async def create_copy(request: Request):
    body = await request.json()
    copy_id = str(uuid.uuid4())
    # A copy's first link is named by the copy itself; its stored file is named by the link.
    copy = {"id": copy_id, "link_id": copy_id, "operation_id": body["operation_id"],
            "company_id": body["company_id"], "firm_name": body["firm_name"],
            "proof_hash": body["proof_hash"], "backup_sha256": body["backup_sha256"],
            "status": "pending", "sent_at": None, "expires_at": None, "fetched": False,
            "created_at": _now()}
    _copies[copy_id] = copy
    return _row(copy) | {"upload_url": f"{STORAGE}/handoffs/{copy_id.replace('-', '')}.upload"}


@app.put("/handoffs/{name}.upload")
async def upload(name: str, request: Request):
    copy = next((c for c in _copies.values() if c["id"].replace("-", "") == name), None)
    if copy is None:
        raise HTTPException(404)
    _files[name] = await request.body()
    copy["status"] = "ready"
    return Response(status_code=200)


@app.post("/handoffs/{copy_id}/send")
async def send(copy_id: str, request: Request):
    copy = _copies.get(copy_id)
    if copy is None or copy["status"] not in ("ready", "sent"):
        raise HTTPException(409, "The copy is not uploaded")
    body = await request.json()
    copy.update(status="sent", sent_at=_now())
    _outbox.append({"copy_id": copy_id, "recipient": body["recipient"],
                    "reply_to": body["reply_to"], "link": body["link"]})
    return _row(copy)


@app.post("/handoffs/{link_id}/verify")
async def verify(link_id: str, request: Request):
    body = await request.json()
    copy = _by_link(link_id, body.get("proof"))
    return {"verified": copy["backup_sha256"] == body.get("backup_sha256")}


@app.post("/handoffs/{link_id}/fetch")
async def fetch(link_id: str, request: Request):
    copy = _by_link(link_id, (await request.json()).get("proof"))
    return {"download_url": f"{STORAGE}/handoffs/{copy['link_id'].replace('-', '')}.bin"}


@app.get("/handoffs/{name}.bin")
async def download(name: str):
    if name not in _files:
        raise HTTPException(404)
    return Response(_files[name], media_type="application/octet-stream")


@app.post("/handoffs/{link_id}/fetched")
async def fetched(link_id: str, request: Request):
    _by_link(link_id, (await request.json()).get("proof"))["fetched"] = True
    return {}


@app.get("/stand-in/outbox")
async def outbox():
    """What the relay emailed, for the suite to follow the link the way a person would."""
    return {"items": _outbox}


# ── Connector keys ───────────────────────────────────────────────────────────

@app.post("/tokens/{connector}")
async def store_key(connector: str, request: Request, x_session_token: str | None = Header(None)):
    _tokens[(_instance(x_session_token), connector)] = await request.json() | {"revision": uuid.uuid4().hex}
    return {}


@app.get("/tokens/{connector}/revision")
async def revision(connector: str, x_session_token: str | None = Header(None)):
    stored = _tokens.get((_instance(x_session_token), connector))
    if stored is None:
        raise HTTPException(404)
    return {"revision": stored["revision"]}


@app.get("/tokens/{connector}/access-token")
async def access_token(connector: str, x_session_token: str | None = Header(None)):
    stored = _tokens.get((_instance(x_session_token), connector))
    if stored is None:
        raise HTTPException(404)
    return {"access_token": stored["consumer_key"], "store_handle": stored["store_url"]}


@app.delete("/tokens/{connector}")
async def remove_key(connector: str, x_session_token: str | None = Header(None)):
    if _tokens.pop((_instance(x_session_token), connector), None) is None:
        raise HTTPException(404)
    return {}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
