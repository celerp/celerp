# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

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


def _shop_key(handle: str | None) -> str:
    """Normalize a Shopify store handle/shop domain for comparison (strip scheme,
    trailing slash, and the .myshopify.com suffix), so a token's `store_handle`
    and a webhook's `shop` compare equal regardless of stored form."""
    s = (handle or "").strip().lower()
    for pre in ("https://", "http://"):
        s = s.removeprefix(pre)
    return s.rstrip("/").removesuffix(".myshopify.com")


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
        # Hold strong refs to fire-and-forget tasks so the loop can't GC them mid-run
        # (a dropped task = a lost proxy response or webhook sync).
        self._bg_tasks: set = set()
        # Resolve local server ports for proxy routing.
        # In Electron builds the ports are dynamic; Electron passes them via
        # CELERP_API_PORT / CELERP_UI_PORT env vars so we don't rely on the
        # config file (which still holds the stale defaults 8000/8080).
        env_api = os.environ.get("CELERP_API_PORT", "")
        env_ui = os.environ.get("CELERP_UI_PORT", "")
        if env_api and env_ui:
            self._api_port: int = int(env_api)
            self._ui_port: int = int(env_ui)
        else:
            from celerp.config import read_config
            cfg = read_config() or {}
            self._ui_port = cfg.get("server", {}).get("ui_port", 8080)
            self._api_port = cfg.get("server", {}).get("api_port", 8000)

    def _local_port_for(self, path: str) -> int:
        """Route a relayed request to the local server that owns the path.

        Browser-facing traffic lives on the UI server — including routes under
        /api/, which are UI-server endpoints (HTMX fragments, image/barcode
        previews, item & bulk actions), NOT the internal data API. The API
        server is an internal backend the UI calls server-side via api_client.
        The one exception: public share links (/share/<token>[/bundle]) are
        anonymous API-app routes, so they must go to the API port — routed to
        the UI server they 404 and every shared link is dead.
        """
        if path == "/share" or path.startswith("/share/"):
            return self._api_port
        return self._ui_port

    def _spawn(self, coro) -> None:
        """Run a coroutine fire-and-forget while holding a strong reference to the
        task (discarded on completion) so it can't be garbage-collected mid-run."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

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

    def _set_status(self, status: str) -> None:
        """Set the relay status and, on a *change*, emit a one-line sentinel on stdout.

        The Electron host reads the spawned API process's stdout and holds a power-save
        assertion while the relay is serving (keeps the Mac awake so an idle sleep can't drop
        the connection - C4). Routing every status transition through here is the single
        producer of that signal; outside Electron the line is just harmless log noise.
        """
        if status == self._relay_status:
            return
        self._relay_status = status
        try:
            print(f"CELERP_RELAY_STATE={status}", flush=True)
        except Exception:
            pass

    # ── Internal ───────────────────────────────────────────────────────

    async def _connect_and_serve(self) -> None:
        log.debug("Connecting to gateway at %s", self._url)
        self._set_status("connecting")
        # Keepalive on: a silently-dead connection (e.g. the host sleeping) raises
        # ConnectionClosed within ~ping_interval+ping_timeout, so _connect_and_serve
        # exits and run()'s backoff loop reconnects — instead of blocking forever on
        # a half-open socket while the relay returns 502.
        # max_size: the relay forwards each proxied HTTP request as ONE base64'd WS
        # message, so the default 1 MiB frame cap silently broke bulk uploads (a ZIP
        # body > ~750 KB overflowed the frame → connection drop → relay 504). 160 MiB
        # covers a ~100 MB body (≈134 MB base64) with margin.
        # Pin the gateway WS to HTTP/1.1 (never offer h2 in the TLS ALPN) so the relay
        # host can be re-fronted by an edge/CDN without a new build ever negotiating h2.
        # Only for wss:// - a plaintext ws:// (local/dev) takes no TLS context.
        _ssl_ctx = None
        if self._url.startswith("wss://"):
            import ssl as _ssl
            _ssl_ctx = _ssl.create_default_context()
            _ssl_ctx.set_alpn_protocols(["http/1.1"])
        async with websockets.connect(
            self._url, ssl=_ssl_ctx, ping_interval=20, ping_timeout=20, max_size=160 * 1024 * 1024
        ) as ws:
            self._ws = ws
            # Read current TOS version from config
            from celerp.config import read_config
            cfg = read_config() or {}
            tos_version = cfg.get("cloud", {}).get("tos_version", "")
            # App version: the Electron wrapper passes the release version via
            # CELERP_APP_VERSION; fall back to the package version. Sent on every
            # connect so the handshake reflects the build that is actually running.
            from celerp import __version__ as _pkg_version
            app_version = os.environ.get("CELERP_APP_VERSION") or _pkg_version
            # Send hello handshake
            await self._send(ws, {
                "type": "hello",
                "id": str(uuid.uuid4()),
                "payload": {
                    "gateway_token": self._token,
                    "instance_id": self._instance_id,
                    "tos_version": tos_version,
                    "version": app_version,
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
        self._set_status("inactive")

    async def _dispatch(self, msg: dict) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        if msg_type == "hello_ack":
            self._set_status("active")
            # Relay returns the canonical instance_id - store it for quota calls
            from celerp.gateway.state import set_session_token, set_instance_id
            canonical_id = payload.get("instance_id", "")
            if canonical_id and canonical_id != self._instance_id:
                log.debug("Gateway: canonical instance_id updated %s -> %s", self._instance_id, canonical_id)
                self._instance_id = canonical_id
            set_instance_id(self._instance_id)
            # Store short-lived session token - required for cloud-gated endpoints
            session_token = payload.get("session_token", "")
            if session_token:
                set_session_token(session_token)
            else:
                log.warning("Gateway hello_ack: no session_token in payload (instance=%s)", self._instance_id)
            log.info("Gateway handshake complete (instance_id=%s)", self._instance_id)
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
                self._set_status("tos_required")
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
            self._spawn(self._handle_proxy_request(payload))

        elif msg_type == "shopify.webhook":
            self._spawn(self._handle_shopify_webhook(payload))

        elif msg_type == "woocommerce.webhook":
            self._spawn(self._handle_woocommerce_webhook(payload))

        elif msg_type == "invoice.payment":
            self._spawn(self._handle_invoice_payment(payload))

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

    async def _handle_shopify_webhook(self, payload: dict) -> None:
        """A Shopify webhook the relay forwarded. Trigger a targeted incremental
        sync for the affected entity on every Shopify-connected company whose store
        matches the webhook's shop (idempotency keys dedupe against the reconcile pass)."""
        topic = payload.get("topic", "")
        shop = payload.get("shop", "")
        data = payload.get("data") or {}
        try:
            import sqlalchemy as sa

            from celerp.connectors.relay_token import fetch_context
            from celerp.connectors.webhooks import WebhookEvent, handle_webhook
            from celerp.db import get_session_ctx
            from celerp.models.connector_config import ConnectorConfig

            async with get_session_ctx() as session:
                rows = await session.execute(
                    sa.select(ConnectorConfig.company_id).where(
                        ConnectorConfig.connector == "shopify"
                    )
                )
                configs = rows.all()

            event = WebhookEvent(platform="shopify", topic=topic, payload=data)
            want = _shop_key(shop)
            for (company_id,) in configs:
                ctx = await fetch_context(company_id, "shopify")
                if ctx is None:
                    continue
                # Guard against a misrouted/stale delivery: only sync when the webhook's
                # shop matches this instance's connected store. (Companies on an instance
                # share one instance-level Shopify token, so this matches on the store,
                # not a specific company — per-company store routing isn't modelled.)
                if want and _shop_key(ctx.store_handle) != want:
                    continue
                await handle_webhook(event, ctx)
        except Exception as exc:
            log.warning("shopify webhook handling failed (topic=%s): %s", topic, exc)

    async def _handle_woocommerce_webhook(self, payload: dict) -> None:
        """A WooCommerce webhook the relay forwarded. The per-install signing
        secret lives here, so the signature is verified locally before a targeted
        sync runs (a forged delivery verifies against no secret and is ignored)."""
        topic = payload.get("topic", "")
        try:
            import base64

            from celerp.connectors.webhooks import dispatch_woocommerce_webhook

            raw = base64.b64decode(payload.get("body_b64") or "")
            signature = payload.get("signature", "")
            await dispatch_woocommerce_webhook(raw, signature, topic)
        except Exception as exc:
            log.warning("woocommerce webhook handling failed (topic=%s): %s", topic, exc)

    async def _handle_invoice_payment(self, payload: dict) -> None:
        """Backup confirmation for an online invoice payment. Records through the same
        idempotent path as the customer-return reconcile, so a replay is a no-op."""
        company_id = payload.get("company_id")
        entity_id = payload.get("entity_id")
        reference = payload.get("reference")
        if not (company_id and entity_id and reference):
            return
        try:
            from celerp.db import get_session_ctx
            from celerp.models.projections import Projection
            from celerp_docs.routes_payments import record_stripe_payment

            async with get_session_ctx() as session:
                row = await session.get(Projection, (company_id, entity_id))
                if row is None:
                    return
                await record_stripe_payment(
                    session, company_id, entity_id, dict(row.state),
                    reference=reference,
                    amount_minor=int(payload.get("amount_minor") or 0),
                    currency=payload.get("currency", "USD"),
                )
        except Exception as exc:
            log.warning("invoice.payment handling failed (entity=%s): %s", entity_id, exc)

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
                    "headers": [["content-type", "text/event-stream"], ["cache-control", "no-cache"]],
                    "body_b64": base64.b64encode(b"data: {}\n\n").decode(),
                },
            })
            return

        # Never proxy a remote request to destructive local-only operations. Remote access
        # serves the normal authenticated UI, but a wipe must require genuine local origin so
        # a compromised broker (even replaying a captured session) cannot trigger it. Account
        # setup is intentionally NOT blocked here - a headless cloud instance is provisioned
        # through this same proxy, so blocking it would break cloud onboarding.
        _local_only_paths = ("/settings/factory-reset",)
        if any(path == p or path.startswith(p + "/") or path.startswith(p + "?") for p in _local_only_paths):
            await self._send(self._ws, {
                "type": "http.response",
                "payload": {
                    "id": request_id,
                    "status": 403,
                    "headers": [["content-type", "application/json"]],
                    "body_b64": base64.b64encode(
                        b'{"detail":"This action must be performed on the local machine."}'
                    ).decode(),
                },
            })
            return

        port = self._local_port_for(path)

        url = f"http://127.0.0.1:{port}{path}"
        if query:
            url = f"{url}?{query}"

        try:
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    content=body,
                )
            resp_body_b64 = base64.b64encode(resp.content).decode() if resp.content else ""
            # Serialize as an ORDERED LIST of pairs (not a dict): a response can carry several
            # Set-Cookie headers (login/refresh emit access_token + refresh_token), and a dict would
            # collapse them into one comma-joined value the browser can't parse — so the refresh
            # cookie never reaches the browser and the session dies at the access token's hard cap.
            # multi_items() keeps each header separate. Drop content-length; the relay recomputes it.
            _skip = {"transfer-encoding", "connection", "keep-alive", "content-length"}
            resp_headers = [
                [k, v] for k, v in resp.headers.multi_items()
                if k.lower() not in _skip
            ]
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
                    "headers": [["content-type", "text/plain"]],
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
