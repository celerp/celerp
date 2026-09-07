# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx

from celerp.capacity import REQUEST_DB_POOL_SIZE

logger = logging.getLogger(__name__)

# The bulk transport carries the handful of long, finite local operations (backup
# bootstrap/import, large attachment transfer) so they never hold an interactive
# connection slot. Two is enough for the concurrent long operations the UI can
# start, and keeping it small guarantees a page fan-out cannot be starved by them.
_BULK_MAX_CONNECTIONS = 2
# How long a request waits for a free pooled connection before failing fast rather
# than hanging: a saturated pool should surface a temporary error, not an
# indefinite stall.
_POOL_ACQUIRE_TIMEOUT = 2.0

# One source of truth for the temporary-failure copy every local client surfaces,
# so the interactive, anonymous, AI, and bulk context managers cannot drift apart.
SATURATION_MESSAGE = (
    "The app is handling too many requests right now. Please try again in a moment."
)
TIMEOUT_MESSAGE = (
    "Request timed out. The server is busy or the payload is too large. "
    "Try again or reduce the batch size."
)


def _connect_message() -> str:
    from ui.config import API_BASE
    return f"Cannot reach API at {API_BASE}. Is the server running?"


class APIError(Exception):
    def __init__(self, status: int, detail: str, data: dict | None = None):
        self.status = status
        self.detail = detail
        self.data = data
        super().__init__(f"API {status}: {detail}")


class _SharedTransport(httpx.AsyncHTTPTransport):
    """A shared bounded transport reused by every per-token AsyncClient.

    httpx.AsyncClient.aclose() (and __aexit__) unconditionally closes its
    transport, including one injected and shared across clients. With per-token
    clients driving this one transport concurrently (asyncio.gather), the first
    client to leave its `async with` block would close the connection pool out
    from under a sibling request still reading its response, surfacing as a
    ReadError. The per-client close is therefore a no-op here: the pool outlives
    every client and is torn down only by shutdown_pool(), called once from the
    UI app shutdown.
    """

    async def aclose(self) -> None:
        return None

    async def __aexit__(self, *exc_info) -> None:
        return None

    async def shutdown_pool(self) -> None:
        await super().aclose()


# One shared bounded connection pool for every UI-to-API request. Each call still
# gets its own cheap AsyncClient wrapper (a per-token Authorization header cannot be
# shared across tokens), but they all drive requests through this single transport,
# whose Limits cap how many connections the UI process can open into the API. Before
# this, every _client()/_anon_client() call built a fresh AsyncClient with its own
# pool, so a burst of requests opened an unbounded number of pools - a contributor to
# the connection-pool exhaustion in the incident. A shared transport (not one shared
# client) is the right shape here because auth differs per request; alt (d) in the
# plan rejected a single global client for exactly that reason.
_shared_transport: _SharedTransport | None = None
_bulk_transport: _SharedTransport | None = None

# Lightweight observability for the UI client path: how many requests were driven
# through the shared pool, logged on shutdown so pool pressure is visible.
_ui_request_count = 0


def _get_transport() -> _SharedTransport:
    """Return the shared interactive transport, building it lazily on first use.

    Its connection ceiling is the API's own request pool size, so an interactive
    page fan-out can never ask the pool for more connections than it holds for
    interactive work.
    """
    global _shared_transport
    if _shared_transport is None:
        _shared_transport = _SharedTransport(
            limits=httpx.Limits(
                max_connections=REQUEST_DB_POOL_SIZE, max_keepalive_connections=8
            ),
        )
    return _shared_transport


def _get_bulk_transport() -> _SharedTransport:
    """Return the shared bulk transport for long, finite local operations.

    Separate and small (two connections) so backup import and large attachment
    transfers cannot consume the interactive pool's slots.
    """
    global _bulk_transport
    if _bulk_transport is None:
        _bulk_transport = _SharedTransport(
            limits=httpx.Limits(
                max_connections=_BULK_MAX_CONNECTIONS, max_keepalive_connections=1
            ),
        )
    return _bulk_transport


def _local_timeout(timeout: float | httpx.Timeout) -> httpx.Timeout:
    """Return a Timeout that always forces the local pool-acquire bound.

    A caller may hand in a bare float or a fully specified httpx.Timeout; either
    way the pool component is overridden to the local bound so no call site can
    accidentally widen or drop it, while any connect/read/write the caller set is
    preserved.
    """
    if isinstance(timeout, httpx.Timeout):
        return httpx.Timeout(
            connect=timeout.connect,
            read=timeout.read,
            write=timeout.write,
            pool=_POOL_ACQUIRE_TIMEOUT,
        )
    return httpx.Timeout(timeout, pool=_POOL_ACQUIRE_TIMEOUT)


def _local_client(
    token: str | None = None,
    *,
    timeout: float | httpx.Timeout = 10.0,
    follow_redirects: bool = True,
    bulk: bool = False,
    headers: dict | None = None,
) -> httpx.AsyncClient:
    """Build an AsyncClient sharing one of the two bounded local transports.

    This is the single place UI code creates a client into the local API, so no
    call site opens its own private pool. Each site keeps control of the behavior
    that differs between them: an optional bearer token, its timeout, whether
    redirects are followed (proxy routes that inspect a raw redirect pass
    follow_redirects=False), and interactive versus bulk transport selection. A
    finite pool-acquire timeout is applied so a saturated pool fails fast instead
    of hanging.
    """
    global _ui_request_count
    _ui_request_count += 1
    from ui.config import API_BASE

    merged_headers = dict(headers or {})
    if token is not None:
        merged_headers["Authorization"] = f"Bearer {token}"

    return httpx.AsyncClient(
        base_url=API_BASE,
        headers=merged_headers or None,
        timeout=_local_timeout(timeout),
        follow_redirects=follow_redirects,
        transport=_get_bulk_transport() if bulk else _get_transport(),
    )


async def close_shared_client() -> None:
    """Close the shared transports if built. Called from the UI app shutdown.

    Safe to call when none was built. Owns BOTH background coordinators: it
    snapshots every in-flight refresh and metadata task, clears their maps and
    caches, bumps the metadata generation so no straggler can repopulate, then
    cancels and awaits every task before tearing down the transports they run on.
    Logs the total request count as observability for how much traffic the
    bounded pool carried this process lifetime."""
    global _shared_transport, _bulk_transport
    refresh_tasks = _refresh_coordinator.tasks_for_shutdown()
    metadata_tasks = _metadata_coordinator.tasks_for_shutdown()
    _refresh_coordinator.clear_maps()
    _metadata_coordinator.invalidate_all()
    all_tasks = refresh_tasks + metadata_tasks
    for task in all_tasks:
        task.cancel()
    if all_tasks:
        await asyncio.gather(*all_tasks, return_exceptions=True)
    transports = [t for t in (_shared_transport, _bulk_transport) if t is not None]
    _shared_transport = None
    _bulk_transport = None
    logger.info("UI API client shutdown: %d requests served via shared pool", _ui_request_count)
    for transport in transports:
        try:
            await transport.shutdown_pool()
        except Exception:
            pass


def _client(token: str, timeout: float | httpx.Timeout = 10.0) -> httpx.AsyncClient:
    return _local_client(token, timeout=timeout, follow_redirects=True, bulk=False)


def _anon_client(timeout: float | httpx.Timeout = 10.0) -> httpx.AsyncClient:
    """Unauthenticated client (no Authorization header)."""
    return _local_client(None, timeout=timeout, follow_redirects=True, bulk=False)


@asynccontextmanager
async def _local_error_mapping():
    """Map httpx transport errors to the shared APIError statuses/copy.

    The order matters: PoolTimeout (pool saturated -> 503 retryable) subclasses
    TimeoutException (slow upstream -> 504), so it is caught first. Every local
    client context manager wraps its body in this one mapping so the four of them
    can never diverge in status or copy.
    """
    try:
        yield
    except httpx.PoolTimeout as exc:
        raise APIError(503, SATURATION_MESSAGE) from exc
    except httpx.TimeoutException as exc:
        raise APIError(504, TIMEOUT_MESSAGE) from exc
    except httpx.ConnectError as exc:
        raise APIError(503, _connect_message()) from exc


@asynccontextmanager
async def _api_client(token: str, timeout: float | httpx.Timeout = 10.0):
    """Authenticated client context manager.

    Converts httpx network errors to APIError so all callers only need to
    handle APIError — no scattered per-function try/except for timeouts or
    connection failures.
    """
    async with _local_error_mapping():
        async with _client(token, timeout=timeout) as c:
            yield c


@asynccontextmanager
async def _anon_api_client(timeout: float | httpx.Timeout = 10.0):
    """Unauthenticated client context manager with the same error mapping."""
    async with _local_error_mapping():
        async with _anon_client(timeout=timeout) as c:
            yield c


@asynccontextmanager
async def _ai_api_client(token: str, session_token: str, timeout: float | httpx.Timeout = 10.0,
                         bulk: bool = False):
    """Authenticated client with X-Session-Token header for AI endpoints.

    AI traffic rides the interactive transport by default; a file upload sets
    ``bulk`` so its finite body drives the small bulk pool instead of holding an
    interactive connection slot, while still carrying the session token.
    """
    async with _local_error_mapping():
        async with _local_client(
            token,
            timeout=timeout,
            follow_redirects=True,
            bulk=bulk,
            headers={"X-Session-Token": session_token},
        ) as c:
            yield c


@asynccontextmanager
async def _bulk_api_client(token: str, timeout: float | httpx.Timeout = 10.0):
    """Authenticated client on the bulk transport for long, finite file transfers.

    Same 503/504/connect mapping as the interactive wrapper, but its requests
    drive the small separate bulk pool so a large upload/download can never hold
    an interactive connection slot.
    """
    async with _local_error_mapping():
        async with _local_client(
            token,
            timeout=timeout,
            follow_redirects=True,
            bulk=True,
        ) as c:
            yield c


def _raise(r: httpx.Response) -> httpx.Response:
    if r.is_redirect:
        raise APIError(r.status_code, f"Unexpected redirect to {r.headers.get('location', '?')}")
    if r.is_error:
        try:
            body = r.json()
        except Exception:
            body = None
        detail = body.get("detail", r.text) if isinstance(body, dict) else r.text
        data = None
        if isinstance(detail, dict) and "message" in detail:
            # Structured detail (message + extras): keep detail a plain string for
            # the sites that render it, carry the full payload on APIError.data.
            # Dict details WITHOUT a message key (e.g. {"errors": [...]} from
            # fulfill/revert/reserve) pass through unchanged - callers json-dump them.
            data = detail
            detail = detail.get("message") or r.text
        elif isinstance(body, dict) and set(body) - {"detail"}:
            # An error body carrying structured fields beyond `detail` (a top-level
            # machine "code" like scan_run_conflict, with a plain-string detail):
            # keep detail the string the sites render, carry the whole body on
            # APIError.data so callers can branch on the code.
            data = body
        if r.status_code == 401:
            # 401 is expected during fresh init / token expiry; not a warning
            logger.debug("API 401: %s", detail)
        elif r.status_code == 409 and detail == "direct_connection_limit":
            # Expected when another session is already active (single-session enforcement);
            # the caller handles it (force-login prompt), so it is not a warning.
            logger.debug("API 409: %s", detail)
        else:
            logger.warning("API %s: %s", r.status_code, detail)
        raise APIError(r.status_code, detail, data=data)
    return r


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

async def batch_import(token: str, path: str, records: list[dict], upsert: bool = False) -> dict:
    """POST a CIF batch import payload to an API path.

    This is intentionally generic so UI routes can reuse it for items/docs/lists/crm/etc.
    Timeout is set high (300s) because large batches involve many DB writes server-side.
    A large import holds its connection for the whole write, so it rides the bulk pool
    rather than pinning an interactive slot a page fan-out needs.
    """
    async with _bulk_api_client(token, timeout=300.0) as c:
        r = _raise(await c.post(path, json={"records": records, "upsert": upsert}))
        return r.json()


# ---------------------------------------------------------------------------
# Auth (no token needed)
# ---------------------------------------------------------------------------

async def bootstrap_status() -> bool:
    """Returns True if the system has been bootstrapped (any user exists).

    Raises APIError(503) if the API connection is refused, or APIError(504) on
    timeout — callers should catch both and render a friendly "API not running"
    page rather than a 500.
    """
    async with _anon_api_client(timeout=5.0) as c:
        r = await c.get("/auth/bootstrap-status")
        if r.is_error:
            return False
        return r.json().get("bootstrapped", False)


async def has_data(token: str) -> bool:
    """Returns True if the company has any inventory/docs/contacts loaded."""
    async with _api_client(token) as c:
        try:
            val = _raise(await c.get("/items/valuation")).json()
            return (val.get("item_count", 0) or 0) > 0
        except APIError:
            return False


async def login(email: str, password: str) -> tuple[str, str]:
    """Returns (access_token, refresh_token)."""
    async with _anon_api_client() as c:
        r = _raise(await c.post("/auth/login", json={"email": email, "password": password}))
        data = r.json()
        return data["access_token"], data["refresh_token"]


async def logout(access_token: str) -> None:
    """Clear all active sessions in the API process (rotates nonce, invalidates all tokens)."""
    try:
        async with _anon_client(timeout=5.0) as c:
            await c.post("/auth/logout", headers={"Authorization": f"Bearer {access_token}"})
    except Exception:
        pass  # best-effort: cookie is cleared regardless


async def login_force(email: str, password: str) -> tuple[str, str]:
    """Like login() but evicts other active sessions first."""
    async with _anon_api_client() as c:
        r = _raise(await c.post("/auth/login-force", json={"email": email, "password": password}))
        data = r.json()
        return data["access_token"], data["refresh_token"]


async def change_password(token: str, current_password: str, new_password: str) -> str:
    """Change password for the authenticated user. Returns detail message."""
    async with _api_client(token) as c:
        r = _raise(await c.post("/auth/change-password", json={
            "current_password": current_password, "new_password": new_password,
        }))
        return r.json()["detail"]


async def setup_code_required() -> bool:
    """True if this (headless) install requires a setup code to create the first admin."""
    async with _anon_api_client(timeout=5.0) as c:
        r = await c.get("/auth/bootstrap-status")
        if r.is_error:
            return False
        return r.json().get("setup_code_required", False)


async def register(company_name: str, email: str, name: str, password: str,
                   setup_code: str | None = None) -> tuple[str, str]:
    """Returns (access_token, refresh_token)."""
    payload = {"company_name": company_name, "email": email, "name": name, "password": password}
    if setup_code:
        payload["setup_code"] = setup_code
    async with _anon_api_client() as c:
        r = _raise(await c.post("/auth/register", json=payload))
        data = r.json()
        return data["access_token"], data["refresh_token"]


# ---------------------------------------------------------------------------
# Single-flight refresh coordinator
# ---------------------------------------------------------------------------
# Several authenticated requests can arrive at once carrying the same refresh
# cookie (a page that fires a handful of concurrent HTMX fragments, all past the
# token's sliding half-life). Each used to run its own POST /auth/token/refresh,
# so N concurrent requests meant N upstream refreshes hammering the auth path at
# exactly the moment the pool is under pressure. The coordinator collapses
# concurrent identical presentations into one upstream POST whose result every
# caller receives.
#
# Keys are the SHA-256 digest of the refresh token, never the raw token: the map
# lives in process memory and the digest is enough to coalesce identical
# presentations without keeping the secret around as a dict key. A failure is
# never cached - only a successful pair is held, and only for a short grace so a
# straggler that arrives microseconds after the winner still coalesces, after
# which the next presentation refreshes again.
import asyncio  # noqa: E402 - kept beside the coordinator it serves
import hashlib  # noqa: E402
import time  # noqa: E402
import copy as _copy  # noqa: E402

_REFRESH_GRACE_SECONDS = 0.5


def _refresh_key(refresh_token: str) -> bytes:
    return hashlib.sha256(refresh_token.encode()).digest()


async def _refresh_upstream(refresh_token: str) -> tuple[str, str]:
    """The raw upstream exchange: POST the refresh token, return the new pair.

    This is the single network call the coordinator coalesces onto; it holds no
    coordination state of its own so it stays trivially testable and mockable.
    """
    async with _anon_api_client() as c:
        r = _raise(await c.post("/auth/token/refresh", json={"refresh_token": refresh_token}))
        data = r.json()
        return data["access_token"], data["refresh_token"]


def _consume_task_exception(task: asyncio.Task) -> None:
    """Retrieve a finished task's exception so the loop never warns about it.

    Registered as a done-callback on every shared coordinator task, so a failed
    orphan (one whose every waiter cancelled before it finished) has its
    exception state read and never surfaces as "Task exception was never
    retrieved".
    """
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


class _SingleFlightCoordinator:
    """One coalescing coordinator, shared by the refresh and metadata caches.

    Concurrent callers presenting the same key share one task. The task, never a
    waiter, owns its own map cleanup: on failure it removes its own inflight
    entry so the next presentation retries; on success it removes its own
    inflight entry and, only if it is still the registered task for the key AND
    the generation captured at its creation still matches, publishes into the
    success/cache map for a short grace/TTL window - so a fetch that started
    before an invalidation can never overwrite fresher state. A waiter is
    shielded from its own cancellation so the shared task keeps running for the
    others. Failures are never cached.

    ``grace_seconds`` is a callable, not a fixed float, so a caller (or a test)
    that reassigns the backing module constant is observed on the coordinator's
    next prune, exactly as the two hand-written coordinators this replaces did.
    """

    def __init__(self, *, grace_seconds, track_tasks: bool = False, copy_on_read: bool = False):
        self.lock = asyncio.Lock()
        self.inflight: dict[bytes, asyncio.Task] = {}
        self.store: dict[bytes, tuple[float, object]] = {}
        self.tasks: set[asyncio.Task] | None = set() if track_tasks else None
        self._grace_seconds = grace_seconds
        self._generation = 0
        self._copy_on_read = copy_on_read

    def _prune_locked(self, now: float) -> None:
        """Drop every store entry older than the grace/TTL. Lock held.

        Pruned globally (every key), not only the one being read, so a digest
        never presented again does not accumulate forever in a long-running
        process.
        """
        grace = self._grace_seconds()
        for key, (created_at, _value) in list(self.store.items()):
            if now - created_at > grace:
                self.store.pop(key, None)

    def clear_maps(self) -> None:
        """Clear the inflight and store maps. Tracked tasks and generation untouched.

        Invalidation and shutdown both call this to drop cached results and
        detach new callers from any old task, but a task already running is
        removed from the tracked set only by its own completion callback or by
        final shutdown - never here. Forgetting a live task here would hide it
        from tasks_for_shutdown(), so close_shared_client could tear down the
        transport the task is still reading on.
        """
        self.inflight.clear()
        self.store.clear()

    def reset_for_tests(self) -> None:
        """Clear inflight, store, and any tracked tasks. For tests only."""
        self.clear_maps()
        if self.tasks is not None:
            self.tasks.clear()

    def invalidate_all(self) -> None:
        """Bump the generation and clear the inflight and store maps. Process-wide.

        No request that starts after this can attach to the old task, and any
        old inflight fetch cannot repopulate the store because its captured
        generation is now stale. A pre-invalidation fetch keeps running and stays
        tracked, so shutdown can still cancel and await it before its transport is
        torn down: invalidation forgets cached results, never live tasks.
        """
        self._generation += 1
        self.clear_maps()

    def tasks_for_shutdown(self) -> list[asyncio.Task]:
        """Every task shutdown must cancel and await.

        A tracked coordinator uses its done-tracked set, a superset of inflight
        that stays populated through the brief window between the task's own
        inflight cleanup and asyncio marking it done. An untracked coordinator
        has no such set and uses inflight directly.
        """
        if self.tasks is not None:
            return list(self.tasks)
        return list(self.inflight.values())

    async def _run(self, key: bytes, generation: int, fetch) -> object:
        current = asyncio.current_task()
        try:
            result = await fetch()
        except BaseException:
            async with self.lock:
                if self.inflight.get(key) is current:
                    self.inflight.pop(key, None)
            raise

        async with self.lock:
            if self.inflight.get(key) is current:
                self.inflight.pop(key, None)
                if generation == self._generation:
                    self.store[key] = (time.monotonic(), result)
        return result

    async def run(self, key: bytes, fetch):
        """Coalesce concurrent identical-key calls onto one shared task.

        A fresh store entry is served directly; a cold or expired key runs
        exactly one shared task even under a concurrent burst, because
        concurrent callers await the same task rather than each starting their
        own.
        """
        async with self.lock:
            now = time.monotonic()
            self._prune_locked(now)
            entry = self.store.get(key)
            if entry is not None and (now - entry[0]) <= self._grace_seconds():
                value = entry[1]
                return _copy.deepcopy(value) if self._copy_on_read else value
            task = self.inflight.get(key)
            if task is None:
                generation = self._generation
                task = asyncio.create_task(self._run(key, generation, fetch))
                if self.tasks is not None:
                    tracked = self.tasks
                    tracked.add(task)

                    def _done(finished: asyncio.Task) -> None:
                        tracked.discard(finished)
                        _consume_task_exception(finished)

                    task.add_done_callback(_done)
                else:
                    task.add_done_callback(_consume_task_exception)
                self.inflight[key] = task

        # shield: this waiter's own cancellation must not cancel the shared task
        # the other waiters are still awaiting. The task, not the waiter, owns
        # cleanup.
        result = await asyncio.shield(task)
        return _copy.deepcopy(result) if self._copy_on_read else result


_refresh_coordinator = _SingleFlightCoordinator(grace_seconds=lambda: _REFRESH_GRACE_SECONDS)
_refresh_inflight = _refresh_coordinator.inflight
_refresh_success = _refresh_coordinator.store


def _reset_refresh_state_for_tests() -> None:
    """Clear the coordinator's module-level maps. For tests only."""
    _refresh_coordinator.reset_for_tests()


async def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """Exchange refresh token for new (access_token, refresh_token), single-flight.

    Concurrent callers presenting the same refresh token share one upstream POST.
    A waiter that is cancelled (its request disconnected) simply raises
    CancelledError; the shared task keeps running and cleans itself up, so the
    other waiters still complete and a later request never replays a dead task.
    Raises APIError on failure, and a failure is never cached.
    """
    key = _refresh_key(refresh_token)
    return await _refresh_coordinator.run(key, lambda: _refresh_upstream(refresh_token))


async def my_companies(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/auth/my-companies")).json()


async def switch_company(token: str, company_id: str) -> tuple[str, str]:
    async with _api_client(token) as c:
        r = _raise(await c.post(f"/auth/switch-company/{company_id}"))
        data = r.json()
        return data["access_token"], data["refresh_token"]


# ---------------------------------------------------------------------------
# Company / settings
# ---------------------------------------------------------------------------

def _flatten_company(data: dict) -> dict:
    """Flatten settings sub-fields into top-level for UI convenience."""
    settings = data.get("settings") or {}
    for k in ("currency", "timezone", "fiscal_year_start", "tax_id", "phone", "address", "vertical", "email",
              "reorder_alerts_enabled", "reorder_alert_email", "inventory_method", "stripe_deposit_account",
              "line_item_identifier"):
        if k not in data:
            data[k] = settings.get(k)
    # Expose dashboard preferences at top level
    dashboard = settings.get("dashboard") or {}
    data["docs_default_preset"] = dashboard.get("docs_default_preset", "last_12m")
    data["default_per_page"] = dashboard.get("per_page", 50)
    return data


async def get_company(token: str) -> dict:
    async with _api_client(token) as c:
        data = _raise(await c.get("/companies/me")).json()
        return _flatten_company(data)


async def get_commercial_state(token: str, timeout: float = 3.0) -> dict:
    """Fetch the live commercial state (feature_flags, commercial_context,
    partner_identity, commercial_mode) the API process holds from the relay.

    Short timeout so a hung API degrades the settings page to neutral quickly
    rather than hanging on the render path."""
    async with _api_client(token, timeout=timeout) as c:
        return _raise(await c.get("/companies/commercial-state")).json()


async def patch_company(token: str, data: dict) -> dict:
    """Patch company. Settings sub-fields and dashboard preferences are merged into
    the settings dict; top-level fields (name, slug) are patched directly."""
    _SETTINGS_FIELDS = {"currency", "timezone", "fiscal_year_start", "tax_id", "phone", "address", "email",
                        "reorder_alerts_enabled", "reorder_alert_email", "inventory_method", "stripe_deposit_account",
                        "line_item_identifier"}
    _DASHBOARD_FIELDS = {"docs_default_preset", "default_per_page"}
    settings_patch = {k: v for k, v in data.items() if k in _SETTINGS_FIELDS}
    dashboard_patch = {}
    # Map default_per_page to per_page for storage
    for k in _DASHBOARD_FIELDS:
        if k in data:
            storage_key = "per_page" if k == "default_per_page" else k
            dashboard_patch[storage_key] = data[k]
    direct_patch = {k: v for k, v in data.items()
                    if k not in _SETTINGS_FIELDS and k not in _DASHBOARD_FIELDS}
    async with _api_client(token) as c:
        if settings_patch or dashboard_patch:
            current = _raise(await c.get("/companies/me")).json()
            merged = {**(current.get("settings") or {}), **settings_patch}
            if dashboard_patch:
                merged["dashboard"] = {**(merged.get("dashboard") or {}), **dashboard_patch}
            _raise(await c.patch("/companies/me", json={"settings": merged}))
        if direct_patch:
            _raise(await c.patch("/companies/me", json=direct_patch))
        raw = _raise(await c.get("/companies/me")).json()
    _invalidate_inventory_metadata()
    return _flatten_company(raw)


async def create_company(token: str, company_name: str) -> str:
    """Create a new company linked to the current user. Returns new JWT scoped to it."""
    async with _api_client(token) as c:
        r = _raise(await c.post("/companies", json={"name": company_name}))
        return r.json()["access_token"]


async def patch_role_permission(token: str, perm_key: str, role_key: str, granted: bool) -> dict:
    """Toggle one permission's minimum role. Owner-gated by the backing endpoint."""
    async with _api_client(token) as c:
        result = _raise(await c.patch(
            "/companies/me/role-permissions",
            json={"perm_key": perm_key, "role_key": role_key, "granted": granted},
        )).json()
    _invalidate_inventory_metadata()
    return result


async def get_item_schema(token: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/item-schema")).json()


async def patch_item_schema(token: str, fields: list[dict]) -> dict:
    async with _api_client(token) as c:
        result = _raise(await c.patch("/companies/me/item-schema", json={"fields": fields})).json()
    _invalidate_inventory_metadata()
    return result


async def get_all_category_schemas(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/category-schemas")).json()


async def get_company_category_schemas(token: str) -> dict:
    """Return only company-applied schemas (no module defaults). Used to determine which categories the user explicitly applied."""
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/company-category-schemas")).json()


async def get_category_display_names(token: str) -> dict:
    """Return display names keyed by category slug."""
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/category-display-names")).json()


async def get_category_schema(token: str, category: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/companies/me/category-schema/{category}")).json()


async def patch_category_schema(token: str, category: str, fields: list[dict]) -> dict:
    async with _api_client(token) as c:
        result = _raise(await c.patch(f"/companies/me/category-schema/{category}", json={"fields": fields})).json()
    _invalidate_inventory_metadata()
    return result


async def merge_category_schemas(token: str, schemas: dict[str, list[dict]]) -> dict:
    """Auto-merge attribute keys from import into category schemas."""
    async with _api_client(token) as c:
        result = _raise(await c.post("/companies/me/category-schemas/merge", json={"schemas": schemas})).json()
    _invalidate_inventory_metadata()
    return result


async def get_column_prefs(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/column-prefs")).json()


async def patch_column_prefs(token: str, prefs: dict[str, list[str]]) -> dict:
    async with _api_client(token) as c:
        result = _raise(await c.patch("/companies/me/column-prefs", json={"prefs": prefs})).json()
    _invalidate_inventory_metadata()
    return result


async def get_locations(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/locations")).json()


async def create_location(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        result = _raise(await c.post("/companies/me/locations", json=data)).json()
    _invalidate_inventory_metadata()
    return result


async def delete_location(token: str, location_id: str) -> dict:
    async with _api_client(token) as c:
        result = _raise(await c.delete(f"/companies/me/locations/{location_id}")).json()
    _invalidate_inventory_metadata()
    return result


async def get_users(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/users")).json()


async def create_user(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/companies/me/users", json=data)).json()


async def patch_user(token: str, user_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/companies/me/users/{user_id}", json=data)).json()


async def get_taxes(token: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/taxes")).json()


async def patch_taxes(token: str, taxes: list[dict]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/taxes", json={"taxes": taxes})).json()


async def get_payment_terms(token: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/payment-terms")).json()


async def patch_payment_terms(token: str, terms: list[dict]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/payment-terms", json={"terms": terms})).json()


async def get_purchasing_taxes(token: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/purchasing-taxes")).json()


async def patch_purchasing_taxes(token: str, taxes: list[dict]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/purchasing-taxes", json={"taxes": taxes})).json()


async def get_purchasing_payment_terms(token: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/purchasing-payment-terms")).json()


async def patch_purchasing_payment_terms(token: str, terms: list[dict]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/purchasing-payment-terms", json={"terms": terms})).json()


async def get_terms_conditions(token: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/terms-conditions")).json()


async def patch_terms_conditions(token: str, templates: list[dict]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/terms-conditions", json={"templates": templates})).json()


async def get_price_lists(token: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/price-lists")).json()


async def patch_price_lists(token: str, price_lists: list[dict], base_price_list: str | None = None) -> dict:
    body: dict = {"price_lists": price_lists}
    if base_price_list is not None:
        # Sent together so renaming the base list stays consistent in one write.
        body["base_price_list"] = base_price_list
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/price-lists", json=body)).json()


async def get_base_price_list(token: str) -> str:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/base-price-list")).json()


async def patch_base_price_list(token: str, name: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/base-price-list", json={"name": name})).json()


async def get_default_price_list(token: str) -> str:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/default-price-list")).json()


async def patch_default_price_list(token: str, name: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/default-price-list", json={"name": name})).json()


async def get_units(token: str) -> list[dict]:
    """GET /companies/me/units → list of unit dicts."""
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/units")).json()


async def patch_units(token: str, units: list[dict]) -> list[dict]:
    """PUT /companies/me/units → replace units list."""
    async with _api_client(token) as c:
        result = _raise(await c.put("/companies/me/units", json={"units": units})).json()
    _invalidate_inventory_metadata()
    return result


# ---------------------------------------------------------------------------
# Inventory static-metadata cache
# ---------------------------------------------------------------------------
# The inventory pages fetch the same six genuinely static getters on every
# load (company/settings is fetched separately and fresh every time, never
# cached here). A short-lived cache collapses those repeated round-trips to one
# per token per window without ever serving a stale snapshot: the TTL is small,
# failures are never cached, and every write that could change the snapshot
# invalidates it centrally in the mutation wrapper, not the route.

from typing import NamedTuple  # noqa: E402

_METADATA_TTL_SECONDS = 5.0


class InventoryMetadata(NamedTuple):
    """One immutable snapshot of the six genuinely static inventory getters.

    Company/settings is deliberately NOT here: it carries authorization state
    (role_grants, enabled_modules) that must never be served from a short-lived,
    per-token UI optimization, so inventory views fetch it fresh on every
    request. Named fields (not a tuple of positional results) so a caller can
    never swap two same-typed results by position. Callers receive defensive
    copies, so a caller mutating what it reads cannot poison the cache.
    """

    item_schema: list[dict]
    category_schemas: dict
    category_display_names: dict
    column_prefs: dict
    locations: dict
    units: list[dict]


def _metadata_key(token: str) -> bytes:
    # SHA-256 of the whole validated access token, never a decoded company_id:
    # the UI does not verify JWT signatures locally, so keying on a claim would
    # let a forged token collide with an authorized entry. A different token is
    # a different key even when it claims the same company.
    return hashlib.sha256(token.encode()).digest()


_metadata_coordinator = _SingleFlightCoordinator(
    grace_seconds=lambda: _METADATA_TTL_SECONDS,
    track_tasks=True,
    copy_on_read=True,
)
_metadata_cache = _metadata_coordinator.store
_metadata_inflight = _metadata_coordinator.inflight
# Every created metadata task, tracked so shutdown can cancel and await them all.
_metadata_tasks = _metadata_coordinator.tasks


def _reset_metadata_cache_for_tests() -> None:
    """Clear the metadata cache maps. For tests only."""
    _metadata_coordinator.reset_for_tests()


async def _fetch_inventory_metadata(token: str) -> InventoryMetadata:
    """One cold fetch: the six static getters gathered once, assembled by name.

    Company is intentionally absent: it is fetched fresh per request outside this
    cache so authorization state is never stale.
    """
    (
        item_schema,
        category_schemas,
        category_display_names,
        column_prefs,
        locations,
        units,
    ) = await asyncio.gather(
        get_item_schema(token),
        get_all_category_schemas(token),
        get_category_display_names(token),
        get_column_prefs(token),
        get_locations(token),
        get_units(token),
    )
    return InventoryMetadata(
        item_schema=item_schema,
        category_schemas=category_schemas,
        category_display_names=category_display_names,
        column_prefs=column_prefs,
        locations=locations,
        units=units,
    )


async def get_inventory_metadata(token: str) -> InventoryMetadata:
    """Return the six static-metadata getters as one snapshot, cached briefly.

    A fresh entry within the TTL is served from cache; a cold key runs exactly
    one gather even under a concurrent burst, because concurrent cold callers
    await a single shared inflight Task rather than each launching their own. The
    shared task owns its own cleanup, so a cancelled waiter never orphans state.
    Failures are never cached. Every returned snapshot is a defensive deep copy
    so a caller mutating what it reads cannot corrupt the stored entry.
    """
    key = _metadata_key(token)
    return await _metadata_coordinator.run(key, lambda: _fetch_inventory_metadata(token))


def _invalidate_inventory_metadata() -> None:
    """Invalidate all cached static metadata process-wide after a settings write.

    A settings write by one authenticated user can change what other users see,
    so invalidating only the writer's token digest would leave others stale.
    Bumping the generation and clearing both maps means no request that starts
    after the write attaches to the old task, and any old inflight fetch cannot
    repopulate the cache because its captured generation is now stale. Existing
    requests already awaiting the old task still finish against their snapshot;
    over-invalidating across companies is acceptable for a five-second cache and
    far safer than cross-token staleness.
    """
    _metadata_coordinator.invalidate_all()


# ---------------------------------------------------------------------------
# Global search
# ---------------------------------------------------------------------------

async def global_search(token: str, q: str) -> dict:
    """Search across items, contacts, docs."""
    async with _api_client(token) as c:
        return _raise(await c.get("/search", params={"q": q})).json()


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def _flatten_item_attrs(item: dict) -> dict:
    """Promote item.attributes keys to the top level so data_table can read them directly.

    Attribute keys never conflict with core item fields (they are schema-defined separately).
    The original ``attributes`` key is preserved for callers that need the nested form.
    """
    attrs = item.get("attributes") or {}
    if not attrs:
        return item
    return {**item, **attrs}


async def list_items(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        raw = _raise(await c.get("/items", params=params or {})).json()
    items = raw.get("items") or []
    return {**raw, "items": [_flatten_item_attrs(i) for i in items]}


async def get_item(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/items/{entity_id}")).json()


# The server caps a single /items/metadata request at MAX_ITEMS_METADATA (5000)
# ids and 422s an over-cap body. A list can hold more unique items than that, so
# split the ids into batches within the cap and merge the per-batch maps. 1000
# keeps each request small while staying well inside the server bound.
ITEMS_METADATA_BATCH = 1000


async def get_items_metadata(token: str, entity_ids: list[str]) -> dict[str, dict]:
    """Bulk item-metadata read: entity_id -> flattened item dict.

    Returns the same per-item shape get_item returns (minus the sold_price
    enrichment). Duplicate ids are collapsed and the request is chunked into
    batches within the server cap, so a list with more unique items than the cap
    still resolves fully. Unknown ids are absent from the map. Callers wrap this in
    their own try/except so a failure degrades to stored line values."""
    ids = list(dict.fromkeys(entity_ids))
    if not ids:
        return {}
    merged: dict[str, dict] = {}
    async with _api_client(token) as c:
        for start in range(0, len(ids), ITEMS_METADATA_BATCH):
            batch = ids[start:start + ITEMS_METADATA_BATCH]
            merged.update(
                (_raise(await c.post("/items/metadata", json={"entity_ids": batch})).json()).get("items", {})
            )
    return merged


async def get_reorder_suggestion(token: str, entity_id: str) -> dict:
    """Velocity-based reorder suggestion: {reorder_point, reorder_qty} (sell units),
    values None when there is no outbound history. Read-only display hint."""
    async with _api_client(token) as c:
        return _raise(await c.get(f"/items/{entity_id}/reorder-suggestion")).json()


async def patch_item(token: str, entity_id: str, fields_changed: dict) -> dict:
    """Patch item fields. Pass a flat {field: value} dict; wraps into {field: {old, new}} format.

    Numeric fields (quantity, *_price) are coerced to float so projection state
    never receives string values from inline edits.
    """
    _NUMERIC = lambda k: k == "quantity" or k.endswith("_price")
    def _coerce(k, v):
        if k == "pieces" and v is not None:
            try:
                return int(float(v))
            except (ValueError, TypeError):
                pass
        if _NUMERIC(k) and v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
        return v
    wrapped = {k: (v if isinstance(v, dict) and "new" in v else {"old": None, "new": _coerce(k, v)}) for k, v in fields_changed.items()}
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/items/{entity_id}", json={"fields_changed": wrapped})).json()


async def upload_attachment(token: str, entity_id: str, file) -> dict:
    async with _bulk_api_client(token) as c:
        content = await file.read() if hasattr(file, "read") else file.file.read()
        filename = getattr(file, "filename", "upload")
        content_type = getattr(file, "content_type", "application/octet-stream") or "application/octet-stream"
        return _raise(await c.post(
            f"/items/{entity_id}/attachments",
            files={"file": (filename, content, content_type)},
        )).json()


async def upload_item_file(token: str, entity_id: str, file) -> dict:
    async with _bulk_api_client(token) as c:
        content = await file.read() if hasattr(file, "read") else file.file.read()
        filename = getattr(file, "filename", "upload")
        content_type = getattr(file, "content_type", "application/octet-stream") or "application/octet-stream"
        return _raise(await c.post(
            f"/items/{entity_id}/files",
            files={"file": (filename, content, content_type)},
        )).json()


async def tag_item_file(token: str, entity_id: str, file_id: str, tag: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(
            f"/items/{entity_id}/files/{file_id}/tag",
            data={"document_tag": tag},
        )).json()


async def describe_item_file(token: str, entity_id: str, file_id: str, description: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(
            f"/items/{entity_id}/files/{file_id}/description",
            data={"description": description},
        )).json()


async def set_item_file_hero(token: str, entity_id: str, file_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/files/{file_id}/hero")).json()


async def delete_item_file(token: str, entity_id: str, file_id: str) -> None:
    async with _api_client(token) as c:
        _raise(await c.delete(f"/items/{entity_id}/files/{file_id}"))


async def delete_attachment(token: str, entity_id: str, att_id: str) -> None:
    async with _api_client(token) as c:
        _raise(await c.delete(f"/items/{entity_id}/attachments/{att_id}"))


async def bulk_attach(token: str, file, override_hero: bool = False) -> dict:
    # Large ZIP upload + per-file processing can take well over the default 10s.
    async with _bulk_api_client(token, timeout=180.0) as c:
        content = await file.read() if hasattr(file, "read") else file.file.read()
        filename = getattr(file, "filename", "attachments.zip")
        params = {"override_hero": "1"} if override_hero else {}
        return _raise(await c.post(
            "/items/files/bulk",
            files={"file": (filename, content, "application/zip")},
            params=params,
        )).json()


async def get_valuation(
    token: str,
    category: str | None = None,
    status: str | None = None,
    on_memo_to: str | None = None,
    consigned_from: str | None = None,
) -> dict:
    params: dict = {}
    if category:
        params["category"] = category
    if status:
        params["status"] = status
    if on_memo_to:
        params["on_memo_to"] = on_memo_to
    if consigned_from:
        params["consigned_from"] = consigned_from
    async with _api_client(token) as c:
        return _raise(await c.get("/items/valuation", params=params)).json()


async def get_item_field_values(token: str, field: str) -> list[str]:
    async with _api_client(token) as c:
        return _raise(await c.get("/items/field-values", params={"field": field})).json().get("values", [])


async def list_item_categories(token: str) -> list[str]:
    async with _api_client(token) as c:
        return _raise(await c.get("/items/categories")).json()


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

async def list_docs(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/docs", params=params or {})).json()


async def list_contact_docs(token: str, contact_id: str, params: dict | None = None) -> dict:
    p = {"contact_id": contact_id, **(params or {})}
    async with _api_client(token) as c:
        return _raise(await c.get("/docs", params=p)).json()


async def get_doc(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/docs/{entity_id}")).json()


async def get_doc_summary(token: str, doc_type: str = "") -> dict:
    params = {}
    if doc_type:
        params["doc_type"] = doc_type
    async with _api_client(token) as c:
        return _raise(await c.get("/docs/summary", params=params)).json()


async def _wrap_fields_changed(c, get_path: str, data: dict) -> dict:
    """Build a fields_changed payload from a flat field->value dict, capturing the entity's
    CURRENT values as ``old`` (one GET) so history shows from -> to, not none -> to. A caller
    may pre-pass a ``{"old","new"}`` dict for any field to supply its own old value; only the
    bare-value fields trigger the lookup, and a failed lookup degrades to ``old: None``."""
    bare = [k for k, v in data.items() if not (isinstance(v, dict) and "new" in v)]
    current: dict = {}
    if bare:
        try:
            current = (await c.get(get_path)).json()
        except Exception:
            current = {}
    return {
        k: (v if isinstance(v, dict) and "new" in v else {"old": current.get(k), "new": v})
        for k, v in data.items()
    }


async def patch_doc(token: str, entity_id: str, data: dict) -> dict:
    """data is a flat dict of field->value; wraps into fields_changed format."""
    async with _api_client(token) as c:
        fields_changed = await _wrap_fields_changed(c, f"/docs/{entity_id}", data)
        return _raise(await c.patch(f"/docs/{entity_id}", json={"fields_changed": fields_changed})).json()


async def renumber_doc(token: str, entity_id: str, ref_id: str) -> dict:
    """Change the display number (ref_id) of any non-void document via /renumber endpoint."""
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/renumber", json={"ref_id": ref_id})).json()


async def create_doc(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/docs", json=data)).json()


async def get_doc_sequences(token: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get("/docs/sequences")).json()


async def patch_doc_sequence(token: str, doc_type: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/docs/sequences/{doc_type}", json=data)).json()


async def finalize_doc(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/finalize")).json()


async def send_doc(token: str, entity_id: str, data: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/send", json=data or {})).json()


async def void_doc(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/void", json={"reason": reason})).json()


async def revert_doc_to_draft(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/revert-to-draft", json={"reason": reason})).json()


async def revert_list_to_draft(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/revert-to-draft", json={"reason": reason})).json()


async def unvoid_doc(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/unvoid", json={})).json()


async def close_doc(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/close", json={"reason": reason})).json()


async def reopen_doc(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/reopen", json={})).json()


async def fulfill_lines(token: str, entity_id: str, line_entity_ids: list[str]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/fulfill-lines", json={"line_entity_ids": line_entity_ids})).json()


async def unfulfill_lines(token: str, entity_id: str, line_entity_ids: list[str],
                          quantities: dict[str, float] | None = None) -> dict:
    """Revert whole lines, or pass quantities={item_id: qty_coming_back} to take back only
    part of a lot; the remainder stays out with the customer."""
    payload: dict = {"line_entity_ids": line_entity_ids}
    if quantities:
        payload["quantities"] = quantities
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/revert-lines", json=payload)).json()


async def reserve_lines(token: str, entity_id: str, line_entity_ids: list[str],
                        new_status: str, is_list: bool = False) -> dict:
    """Set lines reserved or available (ledger-neutral). is_list routes to the list router,
    whose reserve-lines wrapper serves list rows (the docs router 404s them)."""
    base = "/lists" if is_list else "/docs"
    async with _api_client(token) as c:
        return _raise(await c.post(f"{base}/{entity_id}/reserve-lines",
                                   json={"line_entity_ids": line_entity_ids, "new_status": new_status})).json()


async def receive_return(token: str, entity_id: str, items: list[dict], notes: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/receive-return", json={"items": items, "notes": notes})).json()


async def undo_receive_return(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/docs/{entity_id}/receive-return")).json()


async def undo_receive_goods(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/docs/{entity_id}/receive")).json()


async def receive_goods(token: str, entity_id: str, line_items: list[dict], location_id: str | None = None) -> dict:
    payload = {
        "received_items": [
            {
                "sku": li.get("sku", ""),
                "name": li.get("name", "") or li.get("description", ""),
                "quantity_received": float(li.get("quantity", 0) or 0),
                "unit_price": float(li.get("unit_price", 0) or 0),
                "cost_price": float(li.get("cost_price") or li.get("unit_price", 0) or 0),
                "receive_as": li.get("receive_as", "stock"),
                **({"category": li["category"]} if li.get("category") else {}),
                **({"attributes": li["attributes"]} if li.get("attributes") else {}),
            }
            for li in line_items
        ],
        "location_id": location_id or "",
    }
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/receive", json=payload)).json()


async def delete_doc(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/docs/{entity_id}")).json()


async def delete_bulk_drafts(token: str, doc_ids: list[str]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete("/docs/bulk-draft", params={"doc_ids": ",".join(doc_ids)})).json()


async def record_payment(token: str, entity_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/payment", json=data)).json()


# ---------------------------------------------------------------------------
# CRM / contacts
# ---------------------------------------------------------------------------

async def list_contacts(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        data = _raise(await c.get("/crm/contacts", params=params or {})).json()
        # Normalise: backend now returns {items, total}; keep backward compat for callers
        if isinstance(data, list):
            return {"items": data, "total": len(data)}
        return data


async def get_contact(token: str, contact_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/crm/contacts/{contact_id}")).json()


async def patch_contact(token: str, contact_id: str, data: dict) -> dict:
    """data is a flat dict of field->value; wraps into fields_changed format."""
    async with _api_client(token) as c:
        fields_changed = await _wrap_fields_changed(c, f"/crm/contacts/{contact_id}", data)
        return _raise(await c.patch(f"/crm/contacts/{contact_id}", json={"fields_changed": fields_changed})).json()


async def create_contact(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/crm/contacts", json=data)).json()


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------

async def get_chart(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/accounting/chart")).json()


async def seed_chart(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/chart/seed")).json()


async def create_account(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/accounts", json=data)).json()


async def patch_account(token: str, code: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/accounting/accounts/{code}", json=data)).json()


async def get_ledger(token: str, account_code: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/accounting/ledger/{account_code}", params=params or {})).json()


async def get_trial_balance(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/accounting/trial-balance", params=params or {})).json()


async def get_pnl(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/accounting/pnl", params=params or {})).json()


async def get_balance_sheet(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/accounting/balance-sheet", params=params or {})).json()


async def get_journal(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/accounting/journal", params=params or {})).json()


async def get_extended_journal(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/accounting/extended-journal", params=params or {})).json()


async def get_general_ledger(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/accounting/general-ledger", params=params or {})).json()


async def get_soa(token: str, contact_id: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/accounting/soa/{contact_id}", params=params or {})).json()


async def get_cash_flow(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/accounting/cash-flow", params=params or {})).json()


async def create_journal_entry(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/journal-entries", json=data)).json()


async def bulk_void_journal_entries(token: str, je_ids: list[str], reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/journal-entries/bulk-void",
                                   json={"je_ids": je_ids, "reason": reason})).json()


async def get_bank_accounts(token: str, include_inactive: bool = False) -> dict:
    async with _api_client(token) as c:
        params = {"include_inactive": "true"} if include_inactive else {}
        return _raise(await c.get("/accounting/bank-accounts", params=params)).json()


async def get_bank_account(token: str, bank_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/accounting/bank-accounts/{bank_id}")).json()


async def create_bank_account(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/bank-accounts", json=data)).json()


async def patch_bank_account(token: str, bank_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/accounting/bank-accounts/{bank_id}", json=data)).json()


async def create_transfer(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/transfers", json=data)).json()


async def start_reconciliation(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/reconciliation/start", json=data)).json()


async def get_reconciliation(token: str, session_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/accounting/reconciliation/{session_id}")).json()


async def match_reconciliation(token: str, session_id: str, je_ids: list[str]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/match", json={"je_ids": je_ids})).json()


async def complete_reconciliation(token: str, session_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/complete")).json()


async def import_recon_csv(token: str, session_id: str, content: bytes, filename: str, column_map: dict | None = None) -> dict:
    import json as _json
    async with _bulk_api_client(token) as c:
        files = {"file": (filename, content, "text/csv")}
        data = {"column_map": _json.dumps(column_map)} if column_map else {}
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/import-csv", files=files, data=data)).json()


async def get_statement_lines(token: str, session_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/accounting/reconciliation/{session_id}/statement-lines")).json()


async def auto_match_recon(token: str, session_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/auto-match")).json()


async def match_recon_line(token: str, session_id: str, line_id: str, je_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}/match",
            json={"je_id": je_id},
        )).json()


async def unmatch_recon_line(token: str, session_id: str, line_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/lines/{line_id}/unmatch")).json()


async def create_recon_expense(token: str, session_id: str, line_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}/create",
            json=data,
        )).json()


async def split_recon_line(token: str, session_id: str, line_id: str, splits: list[dict]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}/split",
            json={"splits": splits},
        )).json()


async def skip_recon_line(token: str, session_id: str, line_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}",
            json={"status": "skipped"},
        )).json()


async def attach_recon_line(token: str, session_id: str, line_id: str, content: bytes, filename: str) -> dict:
    async with _bulk_api_client(token) as c:
        files = {"file": (filename, content, "application/octet-stream")}
        return _raise(await c.post(
            f"/accounting/reconciliation/{session_id}/lines/{line_id}/attach",
            files=files,
        )).json()


async def bulk_confirm_recon(token: str, session_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/bulk-confirm")).json()


async def write_off_recon(token: str, session_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/accounting/reconciliation/{session_id}/write-off", json=data)).json()


async def get_recon_rules(token: str, bank_account_id: str | None = None) -> dict:
    async with _api_client(token) as c:
        params = {"bank_account_id": bank_account_id} if bank_account_id else {}
        return _raise(await c.get("/accounting/rules", params=params)).json()


async def create_recon_rule(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/rules", json=data)).json()


async def patch_recon_rule(token: str, rule_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/accounting/rules/{rule_id}", json=data)).json()


async def delete_recon_rule(token: str, rule_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/accounting/rules/{rule_id}")).json()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

async def get_ar_aging(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/reports/ar-aging", params=params or {})).json()


async def get_ap_aging(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/reports/ap-aging", params=params or {})).json()


async def get_sales_report(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/reports/sales", params=params or {})).json()


async def get_purchases_report(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/reports/purchases", params=params or {})).json()


async def get_expiring(token: str, days: int = 30) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/reports/expiring", params={"days": days})).json()



# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------

async def list_subscriptions(token: str, params: dict | None = None) -> dict:
    """Returns {items: [...], total: N}."""
    async with _api_client(token) as c:
        return _raise(await c.get("/subscriptions", params=params or {})).json()


async def get_subscription(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/docs/{entity_id}")).json()


async def list_ledger(token: str, params: dict | None = None) -> dict:
    p = dict(params or {})
    p.setdefault("resolve", "true")
    async with _api_client(token) as c:
        return _raise(await c.get("/ledger", params=p)).json()


async def create_subscription(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/docs", json=data)).json()


async def pause_subscription(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/subscriptions/{entity_id}/pause")).json()


async def resume_subscription(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/subscriptions/{entity_id}/resume")).json()


async def cancel_subscription(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/subscriptions/{entity_id}/cancel")).json()


async def generate_subscription(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/subscriptions/{entity_id}/generate")).json()


async def activate_subscription(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/subscriptions/{entity_id}/activate")).json()


# ---------------------------------------------------------------------------
# Manufacturing
# ---------------------------------------------------------------------------

async def list_mfg_orders(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/manufacturing", params=params or {})).json()


async def get_mfg_order(token: str, order_id: str) -> dict:
    """A single production run (raises 404 through APIError for an unknown id)."""
    async with _api_client(token) as c:
        return _raise(await c.get(f"/manufacturing/{order_id}")).json()


async def manufacturing_to_make(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/manufacturing/to-make")).json()


async def manufacturing_make_work_orders(token: str, lines: list[dict], complete: bool = False) -> dict:
    """Create one work order per selected demand line (each {item_id, doc_id}), linked 1:1 to its
    source order, for the line's shortfall. With complete=True, also issue/receive/close each."""
    async with _api_client(token) as c:
        return _raise(await c.post("/manufacturing/to-make/make",
                                   json={"lines": lines, "complete": complete})).json()


async def manufacturing_requirements(token: str, item_ids: list[str]) -> dict:
    """Aggregated raw-material + sub-assembly requirements to make the selected products' shortfall."""
    async with _api_client(token) as c:
        return _raise(await c.post("/manufacturing/to-make/requirements",
                                   json={"item_ids": item_ids})).json()


async def manufacturing_bulk_run_action(token: str, run_ids: list[str], action: str) -> dict:
    """Apply a lifecycle action (start/issue/complete/hold/resume/cancel) to many runs at once."""
    async with _api_client(token) as c:
        return _raise(await c.post("/manufacturing/bulk-action",
                                   json={"run_ids": run_ids, "action": action})).json()


async def manufacturing_item_hub(token: str, item_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/manufacturing/items/{item_id}/hub")).json()




async def set_item_recipe(token: str, entity_id: str, recipe: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.put(f"/manufacturing/items/{entity_id}/recipe", json=recipe)).json()


async def set_item_workflow(token: str, entity_id: str, workflow: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.put(f"/manufacturing/items/{entity_id}/workflow", json=workflow)).json()


async def recost_dependents(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/items/{entity_id}/recost-dependents")).json()


async def build_item(token: str, item_id: str, quantity: float, complete: bool = False) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/items/{item_id}/build",
                                   json={"quantity": quantity, "complete": complete})).json()


async def start_mfg_order(token: str, order_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/start")).json()


async def issue_mfg_order(token: str, order_id: str, items: list[dict] | None = None) -> dict:
    """Issue components into a run (decrements them; auto-advances to In Progress). None = issue all."""
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/issue", json={"items": items})).json()


async def receive_mfg_order(token: str, order_id: str, quantity: float | None = None) -> dict:
    """Receive finished goods from a run as a discrete lot. None = receive all remaining."""
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/receive", json={"quantity": quantity})).json()


async def complete_mfg_order(token: str, order_id: str, data: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/complete", json=data or {})).json()


async def cancel_mfg_order(token: str, order_id: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/cancel", json={"reason": reason})).json()


async def hold_mfg_order(token: str, order_id: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/hold", json={"reason": reason})).json()


async def resume_mfg_order(token: str, order_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/resume")).json()


async def schedule_mfg_order(token: str, order_id: str, fields: dict) -> dict:
    """Set scheduling fields (due_date / planned_start / priority) on a run."""
    async with _api_client(token) as c:
        return _raise(await c.post(f"/manufacturing/{order_id}/schedule", json=fields)).json()


# ── Work Centers (manufacturing master data) ──────────────────────────────────
async def list_work_centers(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/manufacturing/work-centers")).json()


async def create_work_center(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/manufacturing/work-centers", json=data)).json()


async def patch_work_center(token: str, wc_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/manufacturing/work-centers/{wc_id}", json=data)).json()


async def set_default_work_center(token: str, wc_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/manufacturing/work-centers/{wc_id}/is_default")).json()


async def delete_work_center(token: str, wc_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/manufacturing/work-centers/{wc_id}")).json()


async def update_mfg_settings(token: str, mfg: dict) -> dict:
    """Persist the manufacturing settings block under company.settings.manufacturing."""
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me", json={"settings": {"manufacturing": mfg}})).json()


# ---------------------------------------------------------------------------
# BOM
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Scanning disabled — module not yet complete
# ---------------------------------------------------------------------------

# async def scan_once(token: str, code: str, location_id: str | None = None) -> dict:
#     async with _api_client(token) as c:
#         payload: dict = {"code": code}
#         if location_id:
#             payload["location_id"] = location_id
#         return _raise(await c.post("/scanning/scan", json=payload)).json()
#
#
# async def resolve_scan(token: str, code: str) -> dict:
#     async with _api_client(token) as c:
#         return _raise(await c.get(f"/scanning/resolve/{code}")).json()
#
#
# async def start_batch(token: str, location_id: str | None = None) -> dict:
#     async with _api_client(token) as c:
#         return _raise(await c.post("/scanning/batch", json={"location_id": location_id})).json()
#
#
# async def complete_batch(token: str, batch_id: str) -> dict:
#     async with _api_client(token) as c:
#         return _raise(await c.post(f"/scanning/batch/{batch_id}/complete")).json()
#
#
# async def scan_batch(token: str, scans: list[dict]) -> dict:
#     async with _api_client(token) as c:
#         return _raise(await c.post("/scanning/scan/batch", json={"scans": scans})).json()


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

async def _stream_get(token: str, path: str, *, params: dict | None = None,
                      timeout_message: str):
    """GET a streaming endpoint on the BULK transport, body streamed not buffered.
    Returns (chunk_iterator, headers).

    The one streaming helper behind every long/large local download (CSV exports,
    backup archives). Each can run long or reach many GB, so they ride the small
    bulk pool: a slow export or a multi-GB download can never hold one of the
    interactive connection slots ordinary page requests need. The UI must not
    re-read the whole body into memory (that would defeat the backend's streaming
    and pin a connection for the full read); the caller pipes the iterator
    straight into a StreamingResponse, and the httpx client + response stay open
    until the iterator is exhausted. Content-Length is forwarded so the browser
    shows a real download progress bar. PoolTimeout (bulk pool saturated -> 503
    retryable) is caught before TimeoutException (slow upstream -> 504), the same
    order the shared error mapping uses, because PoolTimeout subclasses it."""
    client = _local_client(token, timeout=httpx.Timeout(300.0, connect=10.0), bulk=True)
    try:
        resp = await client.send(client.build_request("GET", path, params=params or {}), stream=True)
    except httpx.PoolTimeout as exc:
        await client.aclose()
        raise APIError(503, SATURATION_MESSAGE) from exc
    except httpx.TimeoutException as exc:
        await client.aclose()
        raise APIError(504, timeout_message) from exc
    except httpx.ConnectError as exc:
        await client.aclose()
        raise APIError(503, _connect_message()) from exc
    if resp.status_code >= 400:
        body = await resp.aread()
        await resp.aclose()
        await client.aclose()
        try:
            import json as _json
            detail = _json.loads(body).get("detail", body.decode("utf-8", "replace"))
        except Exception:
            detail = body.decode("utf-8", "replace")
        raise APIError(resp.status_code, detail)
    headers = {
        k: resp.headers[k]
        for k in ("content-length", "content-disposition", "content-type")
        if k in resp.headers
    }

    async def _iter():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return _iter(), headers


async def export_items_csv(token: str, params: dict | None = None) -> bytes:
    async with _bulk_api_client(token) as c:
        r = _raise(await c.get("/items/export/csv", params=params or {}))
        return r.content


async def export_docs_csv(token: str, params: dict | None = None):
    """GET /docs/export/csv, streamed on the bulk transport. Returns (iter, headers)."""
    return await _stream_get(token, "/docs/export/csv", params=params,
                             timeout_message="The export timed out.")


async def export_contacts_csv(token: str, params: dict | None = None) -> bytes:
    async with _bulk_api_client(token) as c:
        r = _raise(await c.get("/crm/contacts/export/csv", params=params or {}))
        return r.content


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------

async def list_lists(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/lists", params=params or {})).json()


async def get_list(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/lists/{entity_id}")).json()


async def get_list_summary(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/lists/summary")).json()


async def create_list(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/lists", json=data)).json()


async def patch_list(token: str, entity_id: str, data: dict, expected_version: int | None = None) -> dict:
    async with _api_client(token) as c:
        fields_changed = await _wrap_fields_changed(c, f"/lists/{entity_id}", data)
        body: dict = {"fields_changed": fields_changed}
        if expected_version is not None:
            body["expected_version"] = expected_version
        return _raise(await c.patch(f"/lists/{entity_id}", json=body)).json()


async def get_list_page(token: str, entity_id: str, offset: int = 0, limit: int = 100) -> dict:
    """One bounded page of a list's stored lines, with the list header and page metadata.

    Returns {"list": {...stored header without line_items, plus id and version...},
    "items": [...raw stored slice...], "total": int, "version": int,
    "item_meta": {entity_id: flattened item dict}} where items is the raw stored slice
    of positions [offset:offset+limit) and item_meta is the catalog metadata for exactly
    the page's ids (the server enriches it in the same call, so the detail view needs no
    follow-up metadata read). The server hard-caps limit at 100 and applies the effective
    value. An older server that omits item_meta leaves it absent and the caller degrades
    to each line's stored values.
    """
    async with _api_client(token) as c:
        return _raise(await c.get(
            f"/lists/{entity_id}/page",
            params={"offset": offset, "limit": limit},
        )).json()


async def patch_list_line_page(token: str, entity_id: str, page: list[dict], offset: int,
                               original_count: int, expected_version: int | None) -> dict:
    """Save one page slice of a list's lines by position.

    Replaces the stored window [offset:offset+original_count) the client loaded with the
    submitted page: a shorter page deletes tail rows, a longer one inserts. off-window rows
    are untouched. Calls the slice endpoint directly rather than through _wrap_fields_changed,
    so it does not trigger the extra full-list GET that the bare-field patch wrapper performs.
    """
    body: dict = {"line_items": page, "offset": offset, "original_count": original_count,
                  "expected_version": expected_version}
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/lists/{entity_id}/line-page", json=body)).json()


# ── Inventory audits (a list_type=audit on the unified /lists lifecycle) ──────
async def create_audit(token: str, location_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/lists/audit", json={"location_id": location_id})).json()


async def get_audit(token: str, entity_id: str) -> dict:
    return await get_list(token, entity_id)


async def scan_list(token: str, entity_id: str, barcode: str, price_list: str | None = None,
                    run_key: str | None = None) -> dict:
    body: dict = {"barcode": barcode}
    if price_list:
        body["price_list"] = price_list
    if run_key:
        body["run_key"] = run_key
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/scan", json=body)).json()


async def set_audit_count(token: str, entity_id: str, item_id: str, counted_qty: float | None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/lists/{entity_id}/line/{item_id}", json={"counted_qty": counted_qty})).json()


# ── Inventory write-offs (a list_type=writeoff on the unified /lists lifecycle) ──
async def create_writeoff(token: str, entity_ids: list[str]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/lists/writeoff", json={"entity_ids": entity_ids})).json()


async def set_writeoff_line(token: str, entity_id: str, *, line_id: str | None = None, item_id: str | None = None, qty_out: float | None = None, account: str | None = None, comment: str | None = None) -> dict:
    body: dict = {}
    if line_id is not None: body["line_id"] = line_id
    if item_id is not None: body["item_id"] = item_id
    if qty_out is not None: body["qty_out"] = qty_out
    if account is not None: body["account"] = account
    if comment is not None: body["comment"] = comment
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/writeoff-line", json=body)).json()


async def move_transfer(token: str, entity_id: str, to_location_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/move", json={"to_location_id": to_location_id})).json()


async def set_scanned(token: str, entity_id: str, item_ids: list[str] | None = None, scanned: bool = True) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/set-scanned", json={"item_ids": item_ids or [], "scanned": scanned})).json()


async def change_list_type(token: str, entity_id: str, list_type: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/change-type", json={"list_type": list_type})).json()


async def send_list(token: str, entity_id: str, data: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/send", json=data or {})).json()


async def unmark_list_sent(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/unmark-sent")).json()


async def list_action(token: str, entity_id: str, action: str) -> dict:
    """Unified lifecycle/terminal action: finalize, revert-to-draft, adjust, undo-adjust, receive."""
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/{action}")).json()


async def finalize_list(token: str, entity_id: str) -> dict:
    return await list_action(token, entity_id, "finalize")


async def revert_list(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/revert-to-draft", json={})).json()


async def void_list(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/void", json={"reason": reason} if reason else {})).json()


async def delete_list(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/lists/{entity_id}")).json()


async def convert_list(token: str, entity_id: str, target_type: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/convert", json={"target_type": target_type})).json()


async def duplicate_list(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/duplicate", json={})).json()


async def list_doc_notes(token: str, entity_id: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/docs/{entity_id}/notes")).json()


async def add_doc_note(token: str, entity_id: str, note: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/notes", json={"note": note})).json()


async def update_doc_note(token: str, entity_id: str, note_id: str, note: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/docs/{entity_id}/notes/{note_id}", json={"note": note})).json()


async def delete_doc_note(token: str, entity_id: str, note_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/docs/{entity_id}/notes/{note_id}")).json()


async def list_list_notes(token: str, entity_id: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/lists/{entity_id}/notes")).json()


async def add_list_note(token: str, entity_id: str, note: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/lists/{entity_id}/notes", json={"note": note})).json()


async def update_list_note(token: str, entity_id: str, note_id: str, note: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/lists/{entity_id}/notes/{note_id}", json={"note": note})).json()


async def delete_list_note(token: str, entity_id: str, note_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/lists/{entity_id}/notes/{note_id}")).json()


async def export_lists_csv(token: str, params: dict | None = None):
    """GET /lists/export/csv, streamed. Returns (chunk_iterator, headers)."""
    return await _stream_get(token, "/lists/export/csv", params=params,
                             timeout_message="The export timed out.")


# ---------------------------------------------------------------------------
# T1: Document conversion (quotation → invoice)
# ---------------------------------------------------------------------------

async def convert_doc(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/convert")).json()


async def create_shipment_from_docs(token: str, doc_ids: list[str]) -> dict:
    """Create ONE draft shipping document from one or more issued invoices/memos."""
    async with _api_client(token) as c:
        return _raise(await c.post("/docs/shipment", json={"doc_ids": doc_ids})).json()


# ---------------------------------------------------------------------------
# T2: PO receive
# ---------------------------------------------------------------------------

async def receive_po(token: str, entity_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/receive", json=data)).json()


async def return_consignment_items(token: str, entity_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/return-items", json=data)).json()


# ---------------------------------------------------------------------------
# T3: Item actions
# ---------------------------------------------------------------------------

async def adjust_item(token: str, entity_id: str, new_qty: float) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/adjust", json={"new_qty": new_qty})).json()


async def transfer_item(token: str, entity_id: str, location_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/transfer", json={"to_location_id": location_id})).json()


async def set_item_price(token: str, entity_id: str, price_type: str, new_price: float) -> dict:
    # Normalize price_type: accept either raw price list name ("Retail") or
    # conventional key ("retail_price"). Always emit the conventional key so
    # projection state is consistent with the item.pricing.set handler which
    # writes current[price_type] directly.
    if not price_type.endswith("_price"):
        price_type = f"{price_type.lower()}_price"
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/price", json={"price_type": price_type, "new_price": new_price})).json()


async def set_item_status(token: str, entity_id: str, status: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/status", json={"status": status})).json()


async def reserve_item(token: str, entity_id: str, quantity: float, reference: str | None = None) -> dict:
    async with _api_client(token) as c:
        payload: dict = {"quantity": quantity}
        if reference:
            payload["reference"] = reference
        return _raise(await c.post(f"/items/{entity_id}/reserve", json=payload)).json()


async def unreserve_item(token: str, entity_id: str, quantity: float) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/unreserve", json={"quantity": quantity})).json()


async def expire_item(token: str, entity_id: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/expire", json={"reason": reason})).json()



async def set_item_status(token: str, entity_id: str, status: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/items/bulk/status", json={"entity_ids": [entity_id], "status": status, "reason": reason})).json()


async def create_item(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/items", json=data)).json()


async def split_item(token: str, entity_id: str, payload: dict) -> dict:
    """payload: {"children": [...], "mother_qty": float|None, "mother_weight": float|None}"""
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/split", json=payload)).json()


async def transform_item(token: str, entity_id: str, payload: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/{entity_id}/transform", json=payload)).json()


async def split_preview(token: str, entity_id: str, child_sku: str | None = None) -> dict:
    params: dict = {}
    if child_sku:
        params["child_sku"] = child_sku
    async with _api_client(token) as c:
        return _raise(await c.get(f"/items/{entity_id}/split-preview", params=params)).json()


async def merge_items(
    token: str,
    source_entity_ids: list[str],
    target_sku_from: str,
    resulting_quantity: float | None = None,
    resulting_cost_total: float | None = None,
    resulting_name: str | None = None,
    resulting_sku: str | None = None,
    resolved_attributes: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    body: dict = {"source_entity_ids": source_entity_ids, "target_sku_from": target_sku_from}
    if resulting_quantity is not None:
        body["resulting_quantity"] = resulting_quantity
    if resulting_cost_total is not None:
        body["resulting_cost_total"] = resulting_cost_total
    if resulting_name is not None:
        body["resulting_name"] = resulting_name
    if resulting_sku is not None:
        body["resulting_sku"] = resulting_sku
    if resolved_attributes:
        body["resolved_attributes"] = resolved_attributes
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    async with _api_client(token) as c:
        return _raise(await c.post("/items/merge", json=body)).json()


async def bulk_set_status(token: str, entity_ids: list[str], status: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/items/bulk/status", json={"entity_ids": entity_ids, "status": status})).json()


async def make_items_available(token: str, entity_ids: list[str]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/items/bulk/make-available", json={"entity_ids": entity_ids})).json()


async def revert_items_to_draft(token: str, entity_ids: list[str], reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/items/bulk/revert-to-draft", json={"entity_ids": entity_ids, "reason": reason})).json()


async def bulk_shopify_sync(token: str, entity_ids: list[str], enable: bool) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/items/bulk/shopify-sync", json={"entity_ids": entity_ids, "enable": enable})).json()


async def bulk_transfer(token: str, entity_ids: list[str], to_location_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/items/bulk/transfer", json={"entity_ids": entity_ids, "to_location_id": to_location_id})).json()


async def bulk_delete(token: str, entity_ids: list[str]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/items/bulk/delete", json={"entity_ids": entity_ids})).json()


async def bulk_expire(token: str, entity_ids: list[str]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/items/bulk/expire", json={"entity_ids": entity_ids})).json()



# ---------------------------------------------------------------------------
# T5: Deals pipeline
# ---------------------------------------------------------------------------

async def list_deals(token: str, params: dict | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/crm/deals", params=params or {})).json()


async def create_deal(token: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/crm/deals", json=data)).json()


async def move_deal_stage(token: str, deal_id: str, stage: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/crm/deals/{deal_id}/stage", json={"new_stage": stage})).json()


async def mark_deal_won(token: str, deal_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/crm/deals/{deal_id}/won")).json()


async def mark_deal_lost(token: str, deal_id: str, reason: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/crm/deals/{deal_id}/lost", json={"reason": reason})).json()


async def get_deal(token: str, deal_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/crm/deals/{deal_id}")).json()


async def patch_deal(token: str, deal_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/crm/deals/{deal_id}", json=data)).json()


async def delete_deal(token: str, deal_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/crm/deals/{deal_id}")).json()


async def reopen_deal(token: str, deal_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/crm/deals/{deal_id}/reopen")).json()


async def add_contact_tags(token: str, contact_id: str, tags: list[str]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/crm/contacts/{contact_id}/tags", json={"tags": tags})).json()


# ---------------------------------------------------------------------------
# Contact people / addresses / notes
# ---------------------------------------------------------------------------

async def add_contact_person(token: str, contact_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/crm/contacts/{contact_id}/people", json=data)).json()


async def update_contact_person(token: str, contact_id: str, person_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/crm/contacts/{contact_id}/people/{person_id}", json=data)).json()


async def remove_contact_person(token: str, contact_id: str, person_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/crm/contacts/{contact_id}/people/{person_id}")).json()


async def add_contact_address(token: str, contact_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/crm/contacts/{contact_id}/addresses", json=data)).json()


async def update_contact_address(token: str, contact_id: str, address_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/crm/contacts/{contact_id}/addresses/{address_id}", json=data)).json()


async def remove_contact_address(token: str, contact_id: str, address_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/crm/contacts/{contact_id}/addresses/{address_id}")).json()


async def add_contact_note(token: str, contact_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/crm/contacts/{contact_id}/notes", json=data)).json()


async def list_contact_notes(token: str, contact_id: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/crm/contacts/{contact_id}/notes")).json()


async def update_contact_note(token: str, contact_id: str, note_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/crm/contacts/{contact_id}/notes/{note_id}", json=data)).json()


async def delete_contact_note(token: str, contact_id: str, note_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/crm/contacts/{contact_id}/notes/{note_id}")).json()


async def get_contact_tags_vocabulary(token: str) -> list[dict]:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/contact-tags")).json()


async def patch_contact_tags_vocabulary(token: str, tags: list[dict]) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/contact-tags", json={"tags": tags})).json()


async def get_contact_defaults(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/contact-defaults")).json()


async def patch_contact_defaults(token: str, defaults: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch("/companies/me/contact-defaults", json={"defaults": defaults})).json()


async def upload_contact_file(token: str, contact_id: str, file_data: bytes, filename: str, content_type: str, description: str = "", document_tag: str = "") -> dict:
    async with _bulk_api_client(token) as c:
        files = {"file": (filename, file_data, content_type)}
        data = {"description": description, "document_tag": document_tag}
        return _raise(await c.post(f"/crm/contacts/{contact_id}/files", files=files, data=data)).json()


async def tag_contact_file(token: str, contact_id: str, file_id: str, document_tag: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/crm/contacts/{contact_id}/files/{file_id}/tag", data={"document_tag": document_tag})).json()


async def patch_contact_file_description(token: str, contact_id: str, file_id: str, description: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/crm/contacts/{contact_id}/files/{file_id}/description", data={"description": description})).json()


async def delete_contact_file(token: str, contact_id: str, file_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/crm/contacts/{contact_id}/files/{file_id}")).json()


async def download_contact_file(token: str, contact_id: str, file_id: str) -> httpx.Response:
    async with _bulk_api_client(token) as c:
        r = _raise(await c.get(f"/crm/contacts/{contact_id}/files/{file_id}"))
        return r


async def upload_doc_file(token: str, entity_id: str, file_data: bytes, filename: str, content_type: str, description: str = "", document_tag: str = "") -> dict:
    async with _bulk_api_client(token) as c:
        files = {"file": (filename, file_data, content_type)}
        data = {"description": description, "document_tag": document_tag}
        return _raise(await c.post(f"/docs/{entity_id}/files", files=files, data=data)).json()


async def tag_doc_file(token: str, entity_id: str, file_id: str, document_tag: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/docs/{entity_id}/files/{file_id}/tag", data={"document_tag": document_tag})).json()


async def patch_doc_file_description(token: str, entity_id: str, file_id: str, description: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.patch(f"/docs/{entity_id}/files/{file_id}/description", data={"description": description})).json()


async def delete_doc_file(token: str, entity_id: str, file_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/docs/{entity_id}/files/{file_id}")).json()


async def download_doc_file(token: str, entity_id: str, file_id: str) -> httpx.Response:
    async with _bulk_api_client(token) as c:
        return _raise(await c.get(f"/docs/{entity_id}/files/{file_id}"))


async def download_item_file(token: str, entity_id: str, file_id: str) -> httpx.Response:
    async with _bulk_api_client(token) as c:
        return _raise(await c.get(f"/items/{entity_id}/files/{file_id}"))


async def patch_location(token: str, location_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        result = _raise(await c.patch(f"/companies/me/locations/{location_id}", json=data)).json()
    _invalidate_inventory_metadata()
    return result


# ---------------------------------------------------------------------------
# T7: Payment refund
# ---------------------------------------------------------------------------

async def refund_payment(token: str, entity_id: str, data: dict) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/refund", json=data)).json()


async def void_payment(token: str, entity_id: str, payment_index: int, void_reason: str = "") -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/void-payment", json={
            "payment_index": payment_index, "void_reason": void_reason,
        })).json()


async def apply_credit_note(token: str, cn_id: str, target_doc_id: str, amount: float, date: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{cn_id}/apply-to-invoice", json={
            "target_doc_id": target_doc_id, "amount": amount, "date": date,
        })).json()


async def refund_credit_note(token: str, cn_id: str, amount: float, date: str | None = None,
                             method: str | None = None, bank_account: str | None = None,
                             reference: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{cn_id}/cn-refund", json={
            "amount": amount, "date": date, "method": method,
            "bank_account": bank_account, "reference": reference,
        })).json()


async def bulk_payment(token: str, doc_ids: list[str], amount: float, payment_date: str | None = None,
                       method: str | None = None, bank_account: str | None = None,
                       reference: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/docs/bulk-payment", json={
            "doc_ids": doc_ids, "amount": amount, "payment_date": payment_date,
            "method": method, "bank_account": bank_account, "reference": reference,
        })).json()


# ---------------------------------------------------------------------------
# Sprint 6a: Activity feed
# ---------------------------------------------------------------------------

async def get_activity(token: str, limit: int = 15) -> list[dict]:
    async with _api_client(token) as c:
        r = await c.get("/dashboard/activity", params={"limit": limit})
        if r.is_error:
            return []
        return r.json().get("activities", [])


async def search_activity(token: str, *, q: str = "", date_from: str = "", date_to: str = "", page: int = 1, per_page: int = 50) -> dict:
    async with _api_client(token) as c:
        params = {"page": page, "per_page": per_page}
        if q:
            params["q"] = q
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        r = await c.get("/dashboard/activity/search", params=params)
        if r.is_error:
            return {"activities": [], "total": 0, "page": 1, "per_page": per_page, "pages": 1}
        return r.json()

async def get_dashboard_kpis(token: str) -> dict:
    """GET /dashboard/kpis - full KPI payload for vertical-aware dashboard rendering."""
    async with _api_client(token) as c:
        r = await c.get("/dashboard/kpis")
        if r.is_error:
            return {}
        return r.json()


# ---------------------------------------------------------------------------
# Sprint S8: Document share links
# ---------------------------------------------------------------------------

async def get_share_status(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get(f"/docs/{entity_id}/share")).json()


async def get_share_state(token: str, entity_id: str) -> dict:
    """Read-only: {'active': bool}. Does not mint a token (page-load light)."""
    async with _api_client(token) as c:
        return _raise(await c.get(f"/docs/{entity_id}/share/state")).json()


async def create_share_link(token: str, entity_id: str, expires_at: str | None = None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post(f"/docs/{entity_id}/share", json={"expires_at": expires_at})).json()


async def revoke_share_link(token: str, entity_id: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/docs/{entity_id}/share")).json()


async def get_payments_status(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/payments/status")).json()


async def get_payments_enabled(token: str) -> bool:
    """Cached payments flag - cheap per-render gate (no cloud round-trip)."""
    async with _api_client(token) as c:
        return bool(_raise(await c.get("/payments/enabled")).json().get("enabled"))


async def start_payments_connect(token: str) -> dict:
    """Begin Stripe Connect onboarding; returns {url} to redirect the merchant to."""
    async with _api_client(token) as c:
        return _raise(await c.post("/payments/connect")).json()


async def disconnect_payments(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/payments/disconnect")).json()


# ---------------------------------------------------------------------------
# AI assistant
# ---------------------------------------------------------------------------

async def ai_query(token: str, session_token: str, query: str, file_ids: list[str] | None = None) -> dict:
    """POST /ai/query — run an AI query against ERP data.

    session_token must be the gateway-issued X-Session-Token.
    Returns {"answer": str, "model_used": str, "tools_called": list}.
    """
    payload = {"query": query}
    if file_ids:
        payload["file_ids"] = file_ids

    async with _ai_api_client(token, session_token, timeout=60.0) as c:
        return _raise(await c.post("/ai/query", json=payload)).json()


async def ai_conversations_list(token: str, session_token: str) -> list[dict]:
    """GET /ai/conversations - list conversations for sidebar."""
    async with _ai_api_client(token, session_token) as c:
        return _raise(await c.get("/ai/conversations?limit=20")).json()


async def ai_memory_get(token: str, session_token: str) -> dict:
    """GET /ai/memory - get AI memory for current company."""
    async with _ai_api_client(token, session_token) as c:
        return _raise(await c.get("/ai/memory")).json()


async def ai_memory_clear(token: str, session_token: str) -> None:
    """DELETE /ai/memory - clear AI memory for current company."""
    async with _ai_api_client(token, session_token) as c:
        _raise(await c.delete("/ai/memory"))


async def ai_upload(token: str, session_token: str, files: list[tuple[str, bytes, str]]) -> dict:
    """POST /ai/upload - upload files for AI processing.

    files: list of (filename, content_bytes, content_type).
    Returns {"file_ids": [...]}.
    """
    multipart = [("files", (name, data, ct)) for name, data, ct in files]
    async with _ai_api_client(token, session_token, timeout=60.0, bulk=True) as c:
        return _raise(await c.post("/ai/upload", files=multipart)).json()


async def ai_confirm_bills(token: str, session_token: str, bills: list[dict]) -> dict:
    """POST /ai/confirm-bills - confirm and create draft bills proposed by AI."""
    async with _ai_api_client(token, session_token, timeout=60.0) as c:
        return _raise(await c.post("/ai/confirm-bills", json={"bills": bills})).json()


async def ai_usage_stats(token: str, session_token: str = "") -> dict:
    """GET /ai/usage-stats - per-user query/credit usage for current month."""
    if session_token:
        async with _ai_api_client(token, session_token) as c:
            return _raise(await c.get("/ai/usage-stats")).json()
    async with _api_client(token) as c:
        return _raise(await c.get("/ai/usage-stats")).json()


async def ai_quota_status(token: str, session_token: str = "") -> dict:
    """GET /ai/quota-status - get current quota usage for UI badge."""
    if session_token:
        async with _ai_api_client(token, session_token) as c:
            return _raise(await c.get("/ai/quota-status")).json()
    async with _api_client(token) as c:
        return _raise(await c.get("/ai/quota-status")).json()


# ---------------------------------------------------------------------------
# Import history
# ---------------------------------------------------------------------------

async def list_import_batches(token: str) -> dict:
    """GET /items/import/batches — list all import batches for the company."""
    async with _api_client(token) as c:
        return _raise(await c.get("/items/import/batches")).json()


async def undo_import_batch(token: str, batch_id: str) -> dict:
    """POST /items/import/batches/{batch_id}/undo — undo an import batch."""
    async with _api_client(token) as c:
        return _raise(await c.post(f"/items/import/batches/{batch_id}/undo")).json()


# ---------------------------------------------------------------------------
# Module management
# ---------------------------------------------------------------------------

async def get_modules(token: str) -> list[dict]:
    """GET /companies/me/modules — list installed modules with enabled state."""
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/me/modules")).json()


async def enable_module(token: str, module_name: str) -> dict:
    """POST /companies/me/modules/{name}/enable — enable a module (admin only)."""
    async with _api_client(token) as c:
        result = _raise(await c.post(f"/companies/me/modules/{module_name}/enable")).json()
    _invalidate_inventory_metadata()
    return result


async def disable_module(token: str, module_name: str) -> dict:
    """POST /companies/me/modules/{name}/disable — disable a module (admin only)."""
    async with _api_client(token) as c:
        result = _raise(await c.post(f"/companies/me/modules/{module_name}/disable")).json()
    _invalidate_inventory_metadata()
    return result


async def delete_module(token: str, module_name: str) -> dict:
    """POST /companies/me/modules/{name}/delete - remove a disabled, non-default module (admin only)."""
    async with _api_client(token) as c:
        result = _raise(await c.post(f"/companies/me/modules/{module_name}/delete")).json()
    _invalidate_inventory_metadata()
    return result


async def purge_module_data(token: str, module_name: str) -> dict:
    """POST /companies/me/modules/{name}/purge-data - drop the module's prefixed tables (admin only).

    Carries no preview data: the server re-derives the drop list from the manifest
    prefix, so displayed counts are never replayed as input."""
    async with _api_client(token) as c:
        result = _raise(await c.post(f"/companies/me/modules/{module_name}/purge-data")).json()
    _invalidate_inventory_metadata()
    return result


async def import_module_zip(token: str, filename: str, data: bytes,
                            source: str = "sideloaded") -> dict:
    """POST /companies/me/modules/import - install a module from a .zip (admin only).

    `source` records the module's provenance (a plain sideload by default; the
    community-import surface passes "community")."""
    async with _bulk_api_client(token) as c:
        return _raise(await c.post(
            "/companies/me/modules/import",
            files={"file": (filename, data, "application/zip")},
            data={"source": source},
        )).json()


async def import_module_path(token: str, path: str) -> dict:
    """POST /companies/me/modules/import-path - install a module from a local folder."""
    async with _api_client(token) as c:
        return _raise(await c.post("/companies/me/modules/import-path", json={"path": path})).json()


async def buy_module(token: str, slug: str, kind: str, custom_text: str = "") -> dict:
    """POST /companies/me/modules/buy - get a Stripe Checkout URL for a paid module.
    custom_text carries the buyer-language purchase disclosures shown on the
    Checkout page."""
    payload: dict = {"slug": slug, "kind": kind}
    if custom_text:
        payload["custom_text"] = custom_text
    async with _api_client(token) as c:
        return _raise(await c.post("/companies/me/modules/buy", json=payload)).json()


async def module_licenses(token: str) -> list[str]:
    """GET /companies/me/modules/licenses - slugs with an active license."""
    async with _api_client(token) as c:
        r = await c.get("/companies/me/modules/licenses")
        return (r.json().get("licensed", []) or []) if r.status_code == 200 else []


async def marketplace_download(token: str, slug: str) -> dict:
    """POST /companies/me/modules/marketplace-download - fetch a marketplace
    module from the relay and stage it for install (the download can take a
    while). Returns the staged path the following Install reads. The long fetch
    rides the bulk pool so it cannot hold an interactive connection slot."""
    async with _bulk_api_client(token, timeout=90.0) as c:
        return _raise(await c.post("/companies/me/modules/marketplace-download",
                                   json={"slug": slug})).json()


async def marketplace_install(token: str, path: str) -> dict:
    """POST /companies/me/modules/marketplace-install - install a previously
    staged marketplace archive. The module lands disabled, ready to enable."""
    async with _api_client(token) as c:
        return _raise(await c.post("/companies/me/modules/marketplace-install",
                                   json={"path": path})).json()


async def restart_system(token: str) -> dict:
    """POST /system/restart - graceful restart; the process manager respawns."""
    async with _api_client(token) as c:
        return _raise(await c.post("/system/restart")).json()


# ---------------------------------------------------------------------------
# Verticals / Category Library
# ---------------------------------------------------------------------------

async def list_verticals_categories(token: str) -> list[dict]:
    """GET /companies/verticals/categories — list all category definitions."""
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/verticals/categories")).json()


async def list_verticals_presets(token: str) -> list[dict]:
    """GET /companies/verticals/presets — list all vertical presets."""
    async with _api_client(token) as c:
        return _raise(await c.get("/companies/verticals/presets")).json()


async def apply_vertical_preset(token: str, vertical: str) -> dict:
    """POST /companies/me/apply-preset?vertical=X — seed category schemas from a preset."""
    async with _api_client(token) as c:
        result = _raise(await c.post("/companies/me/apply-preset", params={"vertical": vertical})).json()
    _invalidate_inventory_metadata()
    return result


async def apply_vertical_category(token: str, name: str) -> dict:
    """POST /companies/me/apply-category?name=X — seed a single category schema."""
    async with _api_client(token) as c:
        result = _raise(await c.post("/companies/me/apply-category", params={"name": name})).json()
    _invalidate_inventory_metadata()
    return result


async def create_category(token: str, name: str) -> dict:
    """POST /companies/me/categories — create a new empty category."""
    async with _api_client(token) as c:
        result = _raise(await c.post("/companies/me/categories", json={"name": name})).json()
    _invalidate_inventory_metadata()
    return result


async def rename_category(token: str, category_key: str, new_name: str) -> dict:
    """PATCH /companies/me/categories/{key} — rename category and update all item projections."""
    async with _api_client(token) as c:
        result = _raise(await c.patch(f"/companies/me/categories/{category_key}", json={"name": new_name})).json()
    _invalidate_inventory_metadata()
    return result


async def delete_category(token: str, category_key: str) -> dict:
    """DELETE /companies/me/categories/{key} — delete category (403 if items reference it)."""
    async with _api_client(token) as c:
        result = _raise(await c.delete(f"/companies/me/categories/{category_key}")).json()
    _invalidate_inventory_metadata()
    return result


# ── Period Lock + Fiscal Year Close ──────────────────────────────────────────

async def get_period_lock(token: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.get("/accounting/period-lock")).json()


async def set_period_lock(token: str, lock_date: str | None) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/period-lock", json={"lock_date": lock_date})).json()


async def close_fiscal_year(token: str, fiscal_year_end: str) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/accounting/close-year", json={"fiscal_year_end": fiscal_year_end})).json()


async def bulk_delete_contacts(token: str, contact_ids: list) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/crm/contacts/bulk/delete", json={"contact_ids": contact_ids})).json()


async def merge_contacts(token: str, target_contact_id: str, source_contact_ids: list) -> dict:
    async with _api_client(token) as c:
        return _raise(await c.post("/crm/contacts/merge", json={
            "target_contact_id": target_contact_id,
            "source_contact_ids": source_contact_ids,
        })).json()


async def get_relay_status(token: str) -> dict:
    """GET /settings/cloud-status — returns {connected, relay_status, ...}."""
    async with _api_client(token) as c:
        return _raise(await c.get("/settings/cloud-status")).json()


async def get_billing_portal_url(token: str) -> str:
    """POST /settings/cloud/billing-portal - Stripe portal URL for managing the
    Celerp subscription (cancel, change card, invoices)."""
    async with _api_client(token) as c:
        return _raise(await c.post("/settings/cloud/billing-portal")).json()["portal_url"]


async def get_backup_status(token: str) -> dict:
    """GET /settings/backup-status — returns scheduler state from API process."""
    async with _api_client(token) as c:
        return _raise(await c.get("/settings/backup-status")).json()


async def list_backups(token: str) -> dict:
    """GET /backup/list — list cloud snapshots (proxied to the relay by the API)."""
    async with _api_client(token) as c:
        return _raise(await c.get("/backup/list")).json()


async def trigger_backup(token: str) -> None:
    """POST /backup/trigger — trigger an immediate cloud snapshot on the API process.

    A snapshot (pg_dump + dedup upload) can take well over 10 s; use a generous
    timeout so the UI handler gets a real success/error rather than a timeout
    exception that it can't distinguish from a connection failure. The long-held
    request rides the bulk pool so it cannot hold an interactive connection slot.
    """
    async with _bulk_api_client(token, timeout=120.0) as c:
        _raise(await c.post("/backup/trigger"))


async def export_backup(token: str, backup_id: str | None = None):
    """GET /backup/export[/{backup_id}], streamed on the bulk transport.
    Returns (chunk_iterator, headers).

    With no ``backup_id`` this streams a fresh local export; with one it streams a
    cloud snapshot the server reassembles on the fly. Either archive can be many GB
    (DB dump + attachments), so it rides the shared streaming helper on the bulk
    pool: it is never buffered in memory and never holds an interactive connection
    slot. The server builds the archive before the first byte, so the helper's read
    timeout is generous; once bytes flow, each chunk just needs to arrive within it.
    """
    url = f"/backup/export/{backup_id}" if backup_id else "/backup/export"
    return await _stream_get(
        token, url,
        timeout_message="Backup timed out. The archive took too long to build.")


async def disconnect_relay(token: str) -> dict:
    """POST /settings/cloud-disconnect — stop gateway client, clear config."""
    async with _api_client(token) as c:
        return _raise(await c.post("/settings/cloud-disconnect")).json()


async def activate_relay(token: str) -> dict:
    """POST /settings/cloud-activate — call relay /auth/activate, start gateway."""
    async with _api_client(token) as c:
        return _raise(await c.post("/settings/cloud-activate")).json()


async def apply_relay_token(token: str, payload: dict) -> dict:
    """POST /settings/cloud-apply-token — apply pre-fetched gateway token."""
    async with _api_client(token) as c:
        return _raise(await c.post("/settings/cloud-apply-token", json=payload)).json()


async def accept_relay_tos(token: str) -> dict:
    """POST /settings/cloud-accept-tos — persist TOS, restart gateway client."""
    async with _api_client(token) as c:
        return _raise(await c.post("/settings/cloud-accept-tos")).json()


async def get_instance_id(token: str) -> str:
    """GET /settings/cloud-instance-id — return canonical instance_id from API process."""
    async with _api_client(token) as c:
        return _raise(await c.get("/settings/cloud-instance-id")).json()["instance_id"]


async def account_methods(token: str) -> dict:
    """GET /settings/account-methods - optional sign-in methods + Google start URL."""
    async with _api_client(token) as c:
        return _raise(await c.get("/settings/account-methods")).json()


async def account_signup(token: str, email: str) -> dict:
    """POST /settings/account-signup - send the magic sign-in link."""
    async with _api_client(token) as c:
        return _raise(await c.post("/settings/account-signup", json={"email": email})).json()


async def account_status(token: str) -> dict:
    """GET /settings/account-status - poll the relay account state."""
    async with _api_client(token) as c:
        return _raise(await c.get("/settings/account-status")).json()


async def send_otp(token: str, email: str) -> dict:
    """POST /settings/cloud-send-otp — send OTP via API process (correct instance_id)."""
    async with _api_client(token) as c:
        return _raise(await c.post("/settings/cloud-send-otp", json={"email": email})).json()


async def cloud_claim(token: str, payload: dict) -> dict:
    """POST /settings/cloud-claim — claim + activate via API process (correct instance_id)."""
    async with _api_client(token) as c:
        return _raise(await c.post("/settings/cloud-claim", json=payload)).json()


async def get_connectors_catalog(token: str) -> tuple[list[dict], str, bool]:
    """GET /settings/connectors-catalog — proxy relay /api/connectors via API process (has gateway token).
    Returns (connectors, error_detail, needs_plan). error_detail is "" on success;
    needs_plan is True when the relay refused with 402 (no entitled plan)."""
    async with _api_client(token) as c:
        data = _raise(await c.get("/settings/connectors-catalog")).json()
    return data.get("connectors", []), data.get("error", ""), bool(data.get("needs_plan"))


async def get_connector_authorize_url(token: str, platform: str, shop: str = "") -> dict:
    """GET /settings/connectors/{platform}/authorize-url — returns {authorize_url} or {error}."""
    params = {}
    if shop:
        params["shop"] = shop
    async with _api_client(token) as c:
        return _raise(await c.get(f"/settings/connectors/{platform}/authorize-url", params=params)).json()


async def store_connector_credentials(
    token: str, platform: str, consumer_key: str, consumer_secret: str, store_url: str = ""
) -> dict:
    """POST /connectors/{platform}/credentials - validate + store API-key credentials
    via the API process (which holds the relay session). Returns {"ok": True} or
    {"ok": False, "error": <code>, "detail": str}."""
    async with _api_client(token, timeout=20.0) as c:
        return _raise(await c.post(
            f"/connectors/{platform}/credentials",
            json={
                "consumer_key": consumer_key,
                "consumer_secret": consumer_secret,
                "store_url": store_url or None,
            },
        )).json()


async def delete_connector_credentials(token: str, platform: str) -> dict:
    """DELETE /connectors/{platform}/credentials - revoke stored credentials on the
    relay via the API process. Returns {"ok": True} or {"ok": False, "error": <code>}."""
    async with _api_client(token) as c:
        return _raise(await c.delete(f"/connectors/{platform}/credentials")).json()


async def get_connector_access_token(token: str, platform: str) -> dict:
    """GET /connectors/{platform}/access-token - short-lived relay token via the API
    process. Returns {access_token, store_handle, ...} or {"error": <code>, "detail": str}."""
    async with _api_client(token) as c:
        return _raise(await c.get(f"/connectors/{platform}/access-token")).json()
