# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Persistent WebSocket client to relay.celerp.com.

Opt-in: started only when GATEWAY_TOKEN is configured.
A Celerp instance with no GATEWAY_TOKEN never contacts celerp.com.

HTTP proxy: the relay forwards external requests (from <slug>.celerp.com)
over the WS connection. This client handles them locally and returns
the response. No cloudflared or per-customer tunnels needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

_PING_INTERVAL = 30   # seconds
_BACKOFF_MAX = 60     # seconds


class GatewayClient:
    """Persistent outbound WS connection to the Celerp gateway.

    Lifecycle:
        client = GatewayClient(token, instance_id)
        asyncio.create_task(client.run())   # starts connection loop

    The connection loop runs forever with exponential-backoff reconnect.
    Call client.stop() to shut down cleanly.
    """

    def __init__(self, gateway_token: str, instance_id: str, gateway_url: str) -> None:
        self._token = gateway_token
        self._instance_id = instance_id
        self._url = gateway_url
        self._ws: Any = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._relay_status: str = "inactive"  # inactive | connecting | active | tos_required | error
        self._required_tos_version: str = ""
        # Cache local server ports for proxy routing
        from celerp.config import read_config
        cfg = read_config() or {}
        self._ui_port: int = cfg.get("server", {}).get("ui_port", 8080)
        self._api_port: int = cfg.get("server", {}).get("api_port", 8000)

    # ── Public API ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Connection loop with exponential backoff. Runs until stop() is called."""
        self._running = True
        self._stop_event.clear()
        backoff = 1
        while self._running:
            if self._relay_status == "tos_required":
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=1)
                    break  # stop() was called
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._connect_and_serve()
                backoff = 1  # reset on clean disconnect
            except Exception as exc:
                log.warning("Gateway connection lost: %s. Reconnecting in %ds.", exc, backoff)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    break  # stop() was called during backoff
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, _BACKOFF_MAX)

    def stop(self) -> None:
        """Signal the run loop to stop. Call close() from async context for clean WS shutdown."""
        self._running = False
        self._stop_event.set()

    async def close(self) -> None:
        """Async-safe shutdown: signal stop and close the active websocket immediately.

        Call this from async context (e.g. lifespan teardown) instead of stop().
        Awaiting ws.close() unblocks the `async for` receive loop so _connect_and_serve
        exits cleanly, which lets the gateway_task finish without Uvicorn hanging.
        """
        self._running = False
        self._stop_event.set()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    @property
    def relay_status(self) -> str:
        return self._relay_status

    @property
    def required_tos_version(self) -> str:
        return self._required_tos_version

    # ── Internal ───────────────────────────────────────────────────────

    async def _connect_and_serve(self) -> None:
        log.info("Connecting to gateway at %s", self._url)
        self._relay_status = "connecting"
        async with websockets.connect(self._url, ping_interval=_PING_INTERVAL) as ws:
            self._ws = ws
            # Read current TOS version from config
            from celerp.config import read_config
            cfg = read_config() or {}
            tos_version = cfg.get("cloud", {}).get("tos_version", "")
            # Send hello handshake
            await self._send(ws, {
                "type": "hello",
                "id": str(uuid.uuid4()),
                "payload": {
                    "gateway_token": self._token,
                    "instance_id": self._instance_id,
                    "tos_version": tos_version,
                },
            })
            # Message dispatch loop
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("Gateway sent non-JSON frame: %r", raw)
                    continue
                await self._dispatch(msg)
        self._ws = None
        self._relay_status = "inactive"

    async def _dispatch(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        if msg_type == "hello_ack":
            log.info("Gateway handshake complete (instance_id=%s)", self._instance_id)
            self._relay_status = "active"
            # Store short-lived session token - required for cloud-gated endpoints
            session_token = payload.get("session_token", "")
            if session_token:
                from celerp.gateway.state import set_session_token
                set_session_token(session_token)
                log.debug("Gateway session token updated.")
            feature_flags = payload.get("feature_flags", {})
            if feature_flags:
                from celerp.gateway.state import set_feature_flags
                set_feature_flags(feature_flags)
                await self._persist_feature_flags(feature_flags)

        elif msg_type == "session.refresh":
            session_token = payload.get("session_token", "")
            if session_token:
                from celerp.gateway.state import set_session_token
                set_session_token(session_token)
                log.debug("Gateway session token refreshed.")

        elif msg_type == "error":
            code = payload.get("code", "")
            if code == "tos_required":
                self._relay_status = "tos_required"
                self._required_tos_version = payload.get("required_version", "")
                log.warning("Gateway: TOS acceptance required (version=%s)", self._required_tos_version)
            else:
                log.error("Gateway error %s: %s", code, payload.get("message"))

        elif msg_type == "ping":
            if self._ws:
                await self._send(self._ws, {"type": "pong", "id": msg.get("id", "")})

        elif msg_type == "subscription_updated":
            tier = payload.get("tier", "")
            status = payload.get("status", "")
            feature_flags = payload.get("feature_flags", {})
            log.info("Subscription updated: tier=%s status=%s", tier, status)
            if feature_flags:
                from celerp.gateway.state import set_feature_flags
                set_feature_flags(feature_flags)
                await self._persist_feature_flags(feature_flags)
            from celerp.gateway.state import set_subscription_state
            set_subscription_state(tier, status)

        elif msg_type == "http.request":
            asyncio.create_task(self._handle_proxy_request(payload))

        else:
            log.debug("Unhandled gateway message type: %s", msg_type)

    async def _persist_feature_flags(self, feature_flags: dict) -> None:
        """Write feature_flags into Electron's celerp-config.json.

        This is a best-effort operation — it only works when running inside Electron
        where DATA_DIR is set. In dev/server mode this is a no-op.
        """
        import os
        import json
        data_dir = os.environ.get("CELERP_DATA_DIR", "")
        if not data_dir:
            return
        config_path = os.path.join(data_dir, "celerp-config.json")
        try:
            existing: dict = {}
            if os.path.exists(config_path):
                with open(config_path) as f:
                    existing = json.load(f)
            existing["feature_flags"] = feature_flags
            with open(config_path, "w") as f:
                json.dump(existing, f, indent=2)
            log.debug("Gateway: feature_flags persisted to config.")
        except Exception as exc:
            log.warning("Gateway: failed to persist feature_flags: %s", exc)

    async def _handle_proxy_request(self, payload: dict) -> None:
        """Handle a proxied HTTP request from the relay.

        Forwards the request to the local UI server and sends the response
        back over the WS connection.

        SSE/streaming paths are not proxiable (the WS protocol is
        request/response, not streaming). They receive an empty 200 response
        so the browser doesn't error; real-time updates only work on direct
        local access.
        """
        import base64
        import httpx

        request_id = payload.get("id", "")
        method = payload.get("method", "GET")
        path = payload.get("path", "/")
        query = payload.get("query", "")
        headers = payload.get("headers", {})
        body_b64 = payload.get("body_b64", "")
        body = base64.b64decode(body_b64) if body_b64 else None

        # SSE / long-poll paths cannot be proxied over the WS request/response
        # protocol. Return an empty stream so the browser doesn't 500.
        _streaming_paths = ("/notifications/stream",)
        if any(path == p or path.startswith(p) for p in _streaming_paths):
            await self._send(self._ws, {
                "type": "http.response",
                "payload": {
                    "id": request_id,
                    "status": 200,
                    "headers": {"content-type": "text/event-stream", "cache-control": "no-cache"},
                    "body_b64": base64.b64encode(b"data: {}\n\n").decode(),
                },
            })
            return

        # API-only paths go to the API server; everything else to the UI
        if path.startswith("/api/") or path.startswith("/openapi"):
            port = self._api_port
        else:
            port = self._ui_port

        url = f"http://127.0.0.1:{port}{path}"
        if query:
            url = f"{url}?{query}"

        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=body,
                )
            resp_body_b64 = base64.b64encode(resp.content).decode() if resp.content else ""
            # Filter hop-by-hop headers from response
            _skip = {"transfer-encoding", "connection", "keep-alive"}
            resp_headers = {
                k: v for k, v in resp.headers.items()
                if k.lower() not in _skip
            }
            await self._send(self._ws, {
                "type": "http.response",
                "payload": {
                    "id": request_id,
                    "status": resp.status_code,
                    "headers": resp_headers,
                    "body_b64": resp_body_b64,
                },
            })
        except Exception as exc:
            log.warning("Proxy request failed for %s %s: %s", method, path, exc)
            await self._send(self._ws, {
                "type": "http.response",
                "payload": {
                    "id": request_id,
                    "status": 502,
                    "headers": {"content-type": "text/plain"},
                    "body_b64": base64.b64encode(
                        f"Local app error: {type(exc).__name__}: {exc}".encode()
                    ).decode(),
                },
            })

    async def send_message(self, msg_type: str, **payload) -> None:
        """Send a JSON message over the active WS connection.

        Raises RuntimeError if not connected.
        """
        if self._ws is None:
            raise RuntimeError("Not connected to gateway")
        await self._send(self._ws, {"type": msg_type, "id": str(uuid.uuid4()), **payload})

    @staticmethod
    async def _send(ws, message: dict) -> None:
        await ws.send(json.dumps(message))


# Module-level singleton — set by main.py lifespan
_client: GatewayClient | None = None


def get_client() -> GatewayClient | None:
    return _client


def set_client(client: GatewayClient | None) -> None:
    global _client
    _client = client
