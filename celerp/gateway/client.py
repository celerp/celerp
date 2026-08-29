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
import time
import uuid
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

_PING_INTERVAL = 30   # seconds
_BACKOFF_MAX = 60     # seconds
# Consecutive auth rejections before the client stops retrying: a rejected
# credential never heals on its own, so beyond a small allowance for a
# mid-deploy blip every further attempt is noise.
_AUTH_FAILED_MAX = 3
# Lazy free-tier teardown: check this often, and drop the tunnel only after this
# many seconds with no live share AND no proxied request (the grace window debounces
# a revoke-then-reshare so it does not flap connect/disconnect).
_REAP_INTERVAL = 15 * 60   # seconds
_REAP_GRACE = 5 * 60       # seconds


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
        # Reaper wakeup, separate from _stop_event so the teardown timer is
        # independent of the connect/backoff loop's own stop signalling.
        self._reaper_stop = asyncio.Event()
        # Monotonic time of the last proxied request; drives the reaper grace window.
        # 0.0 = none served yet this process.
        self._last_request_monotonic: float = 0.0
        self._relay_status: str = "inactive"  # inactive | connecting | active | tos_required | error
        # One visible line per outage, not one per retry: set on the first
        # failed attempt, cleared by hello_ack so the next outage warns again.
        self._loss_announced = False
        # Consecutive auth rejections; at _AUTH_FAILED_MAX the client goes
        # terminal (_auth_rejected) and run() idles instead of reconnecting.
        self._auth_failures = 0
        self._auth_rejected = False
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
        """Connection loop with exponential backoff. Runs until stop() is called.

        A free instance's tunnel is lazy: a reaper task, owned by this loop so it
        observes the same connection it manages, drops the tunnel once no share is
        live and nothing has been served within the grace window."""
        self._running = True
        self._stop_event.clear()
        self._reaper_stop.clear()
        reaper = asyncio.create_task(self._reaper_loop())
        try:
            backoff = 1
            while self._running:
                if self._relay_status == "tos_required" or self._auth_rejected:
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
                    if self._loss_announced or not self._running or self._auth_rejected:
                        # A failure while stopping is shutdown, not a loss to announce.
                        log.debug("Gateway retry failed: %s. Next attempt in %ds.", exc, backoff)
                    else:
                        self._loss_announced = True
                        log.warning("Relay connection lost. Reconnecting in the background.")
                        log.debug("Gateway connection lost: %s. First retry in %ds.", exc, backoff)
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                        break  # stop() was called during backoff
                    except asyncio.TimeoutError:
                        pass
                    backoff = min(backoff * 2, _BACKOFF_MAX)
        finally:
            self._reaper_stop.set()
            reaper.cancel()
            try:
                await reaper
            except (asyncio.CancelledError, Exception):
                pass

    def stop(self) -> None:
        """Signal the run loop to stop. Call close() from async context for clean WS shutdown."""
        self._running = False
        self._loss_announced = False
        self._auth_failures = 0
        self._auth_rejected = False
        self._stop_event.set()
        self._reaper_stop.set()

    async def close(self) -> None:
        """Async-safe shutdown: signal stop and close the active websocket immediately.

        Call this from async context (e.g. lifespan teardown) instead of stop().
        Awaiting ws.close() unblocks the `async for` receive loop so _connect_and_serve
        exits cleanly, which lets the gateway_task finish without Uvicorn hanging.
        """
        self._running = False
        self._loss_announced = False
        self._auth_failures = 0
        self._auth_rejected = False
        self._stop_event.set()
        self._reaper_stop.set()
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def _should_reap(self) -> bool:
        """A free instance with no live share and no recent proxied request should
        drop its tunnel. A paid instance (public_url set) is never reaped. Best
        effort: any error reading state defers teardown (returns False)."""
        try:
            from celerp.config import settings
            if settings.celerp_public_url:
                return False
            from celerp.gateway import has_active_share
            if await has_active_share():
                return False
            last = self._last_request_monotonic
            if last and (time.monotonic() - last) < _REAP_GRACE:
                return False
            return True
        except Exception:
            log.debug("Gateway reaper: state check failed; deferring teardown", exc_info=True)
            return False

    async def _reaper_loop(self) -> None:
        """Every _REAP_INTERVAL, drop the tunnel once _should_reap() holds. Runs
        alongside the connect loop and exits when stop()/close() is signalled."""
        while self._running:
            try:
                await asyncio.wait_for(self._reaper_stop.wait(), timeout=_REAP_INTERVAL)
                return  # stop requested
            except asyncio.TimeoutError:
                pass
            if not self._running:
                return
            if await self._should_reap():
                log.info("Gateway: no active share and idle past grace; dropping tunnel.")
                await self.close()
                set_client(None)
                return

    @property
    def relay_status(self) -> str:
        return self._relay_status

    def is_serving(self, token: str) -> bool:
        """True when this client is the live tunnel for `token` and has not gone
        dead. A client whose credential the relay rejected (_auth_rejected) or that
        latched the error state idles in run() with no open socket, so a caller that
        just persisted a token must tear it down and rebuild rather than trust the
        set-client no-op. A token mismatch means the persisted credential rotated out
        from under this client."""
        return (
            self._token == token
            and not self._auth_rejected
            and self._relay_status != "error"
        )

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
            self._auth_failures = 0  # an accepted handshake clears the strike count
            # Relay returns the canonical instance_id - store it for quota calls
            from celerp.gateway.state import set_session_token, set_instance_id
            canonical_id = payload.get("instance_id", "")
            if canonical_id and canonical_id != self._instance_id:
                log.debug("Gateway: canonical instance_id updated %s -> %s", self._instance_id, canonical_id)
                self._instance_id = canonical_id
            set_instance_id(self._instance_id)
            # Store the session token exactly as the relay issues it. An empty
            # value is the relay's verdict, not an omission: whatever arrived
            # replaces any stored value, including "".
            session_token = payload.get("session_token", "")
            set_session_token(session_token)
            if not session_token:
                log.debug("Gateway hello_ack: no session token issued (instance=%s)", self._instance_id)
            if self._loss_announced:
                self._loss_announced = False
                log.info("Relay connection restored.")
            else:
                log.info("Relay connected.")
            log.debug("Gateway handshake complete (instance_id=%s)", self._instance_id)
            feature_flags = payload.get("feature_flags", {})
            if feature_flags:
                from celerp.gateway.state import set_feature_flags
                set_feature_flags(feature_flags)
                await self._persist_feature_flags(feature_flags)
            # commercial_context rides hello_ack when present; its absence must
            # leave any cached partner_managed identity intact, so the read is
            # presence-guarded rather than defaulted.
            if "commercial_context" in payload:
                from celerp.gateway.state import set_commercial_context
                ctx = payload["commercial_context"]
                if set_commercial_context(ctx):
                    await self._persist_commercial_context(ctx)
            # tier/status ride hello_ack too (not just subscription_updated): a
            # plain free connection never triggers a Stripe billing event, so
            # that push alone would never tell a free instance its own tier.
            tier = payload.get("tier", "")
            if tier:
                from celerp.gateway.state import set_subscription_state
                set_subscription_state(tier, payload.get("status", ""))

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
            elif code == "auth_failed":
                self._auth_failures += 1
                if self._auth_failures >= _AUTH_FAILED_MAX:
                    self._auth_rejected = True
                    self._set_status("error")
                    log.error(
                        "Relay rejected this installation's saved credentials %d times; "
                        "not retrying. Disconnect and reconnect from Settings, Celerp Connect.",
                        self._auth_failures,
                    )
                else:
                    log.debug("Gateway auth rejected (attempt %d of %d).",
                              self._auth_failures, _AUTH_FAILED_MAX)
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

        elif msg_type == "commercial_updated":
            # The payload is the context itself (flat, like subscription_updated);
            # the same acceptance gate and persist path as the hello_ack branch.
            from celerp.gateway.state import set_commercial_context
            if set_commercial_context(payload):
                await self._persist_commercial_context(payload)

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

    async def _persist_commercial_context(self, ctx: dict) -> None:
        """Write commercial_context into Electron's celerp-config.json.

        Best-effort: only works inside Electron where CELERP_DATA_DIR is set; a
        no-op in dev/server mode. Read-merges the single key so the existing
        feature_flags entry is preserved, and writes atomically (a temp file on
        the same directory then os.replace) so a crash mid-write never leaves a
        torn config for the next startup read.
        """
        data_dir = os.environ.get("CELERP_DATA_DIR", "")
        if not data_dir:
            return
        config_path = os.path.join(data_dir, "celerp-config.json")
        tmp_path = f"{config_path}.{uuid.uuid4().hex}.tmp"
        try:
            existing: dict = {}
            if os.path.exists(config_path):
                with open(config_path) as f:
                    existing = json.load(f)
            existing["commercial_context"] = ctx
            with open(tmp_path, "w") as f:
                json.dump(existing, f, indent=2)
            os.replace(tmp_path, config_path)
            log.debug("Gateway: commercial_context persisted to config.")
        except Exception as exc:
            log.warning("Gateway: failed to persist commercial_context: %s", exc)
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

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

        # Activity stamp for the reaper's grace window: any proxied request counts.
        self._last_request_monotonic = time.monotonic()

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
