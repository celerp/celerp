# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Aggregated global search across first-party modules.

Every searchable module contributes a `search_provider` slot naming a read-only
handler, the permission that gates it, and the key its result list lives under.
This route authenticates once, then runs each contributing provider in turn on a
single database session, so the search box asks each module the same question
and returns one merged answer.

Providers run SEQUENTIALLY on one session, never concurrently: they share the
request's AsyncSession, which is not safe under concurrent use, and a failing
provider must be able to roll that session back cleanly before the next runs.

Failure is contained per provider. A module whose permission the caller lacks,
or whose permission key is unknown, is omitted (never surfaced as an error). A
provider that fails to resolve or raises while running is reported in
`degraded_modules` and does not take the others down. Only the module name and
the exception class are ever logged: never the query text, never a token, never
the exception message.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.modules import slots
from celerp.modules.registry import get_enabled
from celerp.modules.slots import resolve_handler
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.permissions import get_current_company_settings, role_has_permission

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user)])

# The most a single provider contributes to the merged answer. Fixed by the
# aggregator, never taken from the request, so the search box cannot ask a
# module to read its whole table.
_PER_PROVIDER_LIMIT = 5

# The shortest query worth running. One or zero characters match almost
# everything, so the aggregator answers empty without waking any provider.
_MIN_Q_LEN = 2

# The longest query the aggregator accepts, guarding every provider at once
# against a pathological pattern. A longer query is refused deterministically,
# before any provider runs.
_MAX_Q_LEN = 200

# The most wall-clock time one provider may take before it is degraded.
_PROVIDER_TIMEOUT_SECONDS = 1.25

# The total wall-clock budget the aggregator spends across ALL providers.
# search_provider is a public slot, so the provider count is unbounded: a
# per-provider timeout alone lets the serial total grow with the number of slow
# providers, overrunning the 10 second local API request timeout, 504-ing the
# whole search and discarding the buckets that already succeeded. This fixed
# deadline caps the total: each provider gets min(_PROVIDER_TIMEOUT_SECONDS,
# remaining budget), and once the budget is spent the remaining eligible
# providers are degraded without running. 7.5 seconds leaves headroom under the
# 10 second client timeout for response encoding and transport.
_AGGREGATE_DEADLINE_SECONDS = 7.5

# Bounds on a third-party canonical row. A first-party provider is trusted to
# return rich domain rows the bundled UI renders directly; a third-party provider
# is held to exactly these keys so an untrusted module cannot inject arbitrary
# fields, off-site links, or unbounded text into the shared search bar.
_CANONICAL_ROW_KEYS = {"id", "label", "href", "subtitle"}
_LABEL_MAX = 200
_SUBTITLE_MAX = 300
_HREF_MAX = 2048


def _local_href(href) -> bool:
    """True only for a non-empty, bounded, app-local path.

    Rejects any scheme, protocol-relative "//host", a backslash (browsers
    normalise "\\" to "/", so "/\\evil.example" resolves off-site), any ASCII
    control char, and anything that does not start with a single leading slash,
    so a third-party provider can only link within this app, never off-site.

    The app-local rule mirrors ui.security.is_app_local_path, which is the
    single source of truth for app-local path safety. The API layer cannot
    import the UI layer, so the rule is duplicated here and the two must change
    together; this function additionally bounds the length for the third-party
    row contract.
    """
    return (
        isinstance(href, str)
        and 0 < len(href) <= _HREF_MAX
        and href.startswith("/")
        and not href.startswith("//")
        and "\\" not in href
        and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in href)
    )


def _canonical_third_party_row(row: dict) -> dict:
    """Reduce a third-party row to the canonical shape or raise.

    Returns a fresh dict holding exactly the canonical keys (subtitle only when
    present). Any shape violation raises ValueError, which the caller treats as a
    provider failure and degrades the whole provider.
    """
    if not isinstance(row, dict):
        raise ValueError("third-party row must be an object")
    rid = row.get("id")
    if not isinstance(rid, str) or not rid:
        raise ValueError("third-party row id must be a non-empty string")
    label = row.get("label")
    if not isinstance(label, str) or not label or len(label) > _LABEL_MAX:
        raise ValueError("third-party row label must be a non-empty bounded string")
    href = row.get("href")
    if not _local_href(href):
        raise ValueError("third-party row href must be a bounded app-local path")
    clean = {"id": rid, "label": label, "href": href}
    subtitle = row.get("subtitle")
    if subtitle is not None:
        if not isinstance(subtitle, str) or len(subtitle) > _SUBTITLE_MAX:
            raise ValueError("third-party row subtitle must be a bounded string")
        clean["subtitle"] = subtitle
    return clean


def _jsonable_rows(rows: list) -> list[dict]:
    """Run the final capped row list through jsonable_encoder and shape-check it.

    Applied to both the first-party and third-party paths so a row containing a
    value FastAPI's response encoder cannot serialize is caught here, inside the
    provider's own try/except, and degrades only that provider. Without this,
    such a row reaches the aggregate response untouched and FastAPI's own final
    response encoding 500s the whole request after every provider has already
    run. Raises ValueError on anything that fails to encode or does not come
    back as a list of dicts; the caller's existing except Exception treats that
    exactly like any other provider failure.
    """
    encoded = jsonable_encoder(rows)
    if not isinstance(encoded, list) or not all(isinstance(row, dict) for row in encoded):
        raise ValueError("provider rows did not encode to a list of objects")
    return encoded


@router.get("/search")
async def global_search(
    request: Request,
    q: str = "",
    company_id=Depends(get_current_company_id),
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Search every module the caller may see and merge the results.

    Response shape:
        {"results": {module: {result_key: [...]}}, "degraded_modules": [...]}

    A module is present in `results` only when the caller holds its permission
    and its provider returned cleanly. A module the caller cannot see is omitted
    entirely and is NOT degraded. A provider that failed is named in
    `degraded_modules` with no partial results.
    """
    stripped = (q or "").strip()

    if len(stripped) > _MAX_Q_LEN:
        # Invalid input, not an empty search: answer 422 before waking any
        # provider so the UI renders a real error instead of "no results".
        raise HTTPException(
            status_code=422,
            detail=f"Search text must be at most {_MAX_Q_LEN} characters.",
        )

    if len(stripped) < _MIN_Q_LEN:
        # Too short to run: answer empty without waking any provider.
        return {"results": {}, "degraded_modules": []}

    # Per-company module enablement, read from the fresh company settings (never
    # the stale JWT claim). A registered provider slot means the module is loaded
    # in THIS process, not that this company enabled it. When the key is present
    # every provider is gated on it; a present-but-malformed value yields an empty
    # set from get_enabled, which fails closed (show nothing). When the key is
    # absent entirely, a company predating per-module enablement falls back to
    # running every permitted provider.
    enabled_key_present = "enabled_modules" in settings
    enabled_modules = get_enabled(settings)

    results: dict[str, dict] = {}
    degraded_modules: list[str] = []
    rollback_failed = False

    # Shared wall-clock budget across every provider (see _AGGREGATE_DEADLINE_SECONDS).
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _AGGREGATE_DEADLINE_SECONDS

    for contribution in slots.get("search_provider"):
        module = contribution.get("_module") or "?"

        # Disabled for this company: not shown and not degraded (it is off, not
        # broken), and never invoked.
        if enabled_key_present and module not in enabled_modules:
            continue

        # Authorization, failing closed. An unknown permission key raises KeyError
        # from the registry lookup; any exception here omits the provider (it is
        # not degraded, it is simply not shown) and logs only the module name.
        try:
            permitted = role_has_permission(settings, role, contribution["permission"])
        except Exception as exc:
            log.warning(
                "search: skipping provider %s, permission gate raised %s",
                module, type(exc).__name__,
            )
            continue
        if not permitted:
            continue

        # A rollback already failed on an earlier provider, so the session is no
        # longer trustworthy: degrade the rest rather than run them on it.
        if rollback_failed:
            degraded_modules.append(module)
            continue

        # The caller navigated away mid-search: stop spending work on providers
        # whose answer no one is waiting for.
        if await request.is_disconnected():
            break

        # The shared budget is spent: the caller is still waiting, so this is an
        # honest partial answer, not a disconnect. Degrade every remaining
        # eligible provider without running it and return what completed.
        remaining_budget = deadline - loop.time()
        if remaining_budget <= 0:
            degraded_modules.append(module)
            continue

        handler_path = contribution.get("handler")
        result_key = contribution.get("result_key")

        # Resolution failure happens BEFORE any database work, so there is nothing
        # to roll back: degrade the module and move on.
        try:
            handler = resolve_handler(handler_path)
        except Exception as exc:
            log.warning(
                "search: provider %s failed to resolve, %s",
                module, type(exc).__name__,
            )
            degraded_modules.append(module)
            continue

        # Invocation may have touched the session before it raised, so this branch
        # rolls back. If the rollback itself fails the session is unusable, so the
        # remaining providers are degraded and the loop stops.
        try:
            # A provider that hangs rather than raises would otherwise let the
            # whole aggregate request time out before earlier buckets return; its
            # own timeout raises TimeoutError, taking the same degrade+rollback
            # path as any provider failure. Outer request cancellation is a
            # BaseException and is not caught here, so it propagates as it should.
            payload = await asyncio.wait_for(
                handler(session, company_id, role, stripped, _PER_PROVIDER_LIMIT),
                timeout=min(_PROVIDER_TIMEOUT_SECONDS, remaining_budget),
            )
            hits = payload[result_key]
            if not isinstance(hits, list):
                raise TypeError(f"provider returned non-list for {result_key!r}")
            capped = hits[:_PER_PROVIDER_LIMIT]
            if contribution.get("_first_party"):
                # Bundled provider: trusted to return rich domain rows the UI
                # renders directly, passed through untouched other than the
                # jsonable_encoder guard below.
                rows = capped
            else:
                # Third-party provider: hold every row to the canonical shape,
                # which yields plain string fields that are always
                # JSON-serializable. Any violation raises and degrades this
                # provider, never the aggregate response.
                rows = [_canonical_third_party_row(row) for row in capped]
            # Final guard on both paths: a row containing a value the response
            # encoder cannot serialize must degrade this provider now, not turn
            # the whole aggregate response into a 500 once every provider has
            # already run.
            rows = _jsonable_rows(rows)
            results[module] = {result_key: rows}
        except Exception as exc:
            log.warning(
                "search: provider %s raised %s",
                module, type(exc).__name__,
            )
            degraded_modules.append(module)
            try:
                await session.rollback()
            except Exception as rb_exc:
                log.warning(
                    "search: rollback after provider %s raised %s, degrading remaining providers",
                    module, type(rb_exc).__name__,
                )
                rollback_failed = True

    return {"results": results, "degraded_modules": degraded_modules}
