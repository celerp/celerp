# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import json

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.cloud_gate import (
    subscribe_url,
    topup_url,
    is_partner_managed,
    direct_price,
)
from ui.components.shell import base_shell
from ui.config import get_token as _token, get_role
from ui.i18n import t, get_lang


# ---------------------------------------------------------------------------
# Showcase scenario data
# ---------------------------------------------------------------------------

def _get_scenarios(lang: str = "en") -> list[dict]:
    return [
        {
            "id": "batch-bills",
            "label": t("ai.scenario_batch_bills_label", lang),
            "user": t("ai.scenario_batch_bills_user", lang),
            "thinking": t("ai.scenario_batch_bills_thinking", lang),
            "reply": t("ai.scenario_batch_bills_reply", lang),
        },
        {
            "id": "smart-restock",
            "label": t("ai.scenario_smart_restock_label", lang),
            "user": t("ai.scenario_smart_restock_user", lang),
            "thinking": t("ai.scenario_smart_restock_thinking", lang),
            "reply": t("ai.scenario_smart_restock_reply", lang),
        },
        {
            "id": "discrepancy-audit",
            "label": t("ai.scenario_discrepancy_audit_label", lang),
            "user": t("ai.scenario_discrepancy_audit_user", lang),
            "thinking": t("ai.scenario_discrepancy_audit_thinking", lang),
            "reply": t("ai.scenario_discrepancy_audit_reply", lang),
        },
        {
            "id": "bulk-catalog",
            "label": t("ai.scenario_bulk_catalog_label", lang),
            "user": t("ai.scenario_bulk_catalog_user", lang),
            "thinking": t("ai.scenario_bulk_catalog_thinking", lang),
            "reply": t("ai.scenario_bulk_catalog_reply", lang),
        },
    ]


def _get_example_queries(lang: str = "en") -> list[dict]:
    return [
        {"icon": "📎", "title": t("ai.query_receipts_title", lang), "query": t("ai.query_receipts_query", lang), "needs_files": True},
        {"icon": "📦", "title": t("ai.query_restock_title", lang), "query": t("ai.query_restock_query", lang), "needs_files": False},
        {"icon": "🔍", "title": t("ai.query_audit_title", lang), "query": t("ai.query_audit_query", lang), "needs_files": False},
        {"icon": "📊", "title": t("ai.query_summary_title", lang), "query": t("ai.query_summary_query", lang), "needs_files": False},
    ]


# ---------------------------------------------------------------------------
# Quota-exceeded upgrade / top-up card
# ---------------------------------------------------------------------------

def _quota_exceeded_card(detail: dict, user_bubble: FT, lang: str = "en") -> tuple[FT, FT, FT]:
    """The card shown when an AI query is refused for hitting the quota.

    Routes both destinations through the commercial policy: the top-up CTA via
    ``topup_url()`` and the upgrade CTA via ``subscribe_url("ai")``. A
    relay-supplied ``detail.upgrade_url`` is never used - the resolver alone
    decides the destination, so a partner-managed install is never sent to a
    direct Celerp checkout. The upgrade label's direct price is suppressed under
    partner_managed.
    """
    limit = int(detail.get("base_limit", detail.get("limit", 0)) or 0)
    is_ai_tier = detail.get("tier") in ("cloud", "ai", "team")
    if is_ai_tier:
        return (
            user_bubble,
            _msg_bubble("ai", f"You've used all {limit} included AI queries this period."),
            Div(
                P(t("msg.need_more_topup_credits_never_expire", lang), cls="ai-upgrade-label"),
                A(t("msg.buy_more_credits", lang),
                  href=topup_url(),
                  target="_blank", cls="btn btn--primary"),
                cls="ai-upgrade-cta",
            ),
        )
    upgrade_label = t("msg.get_the_ai_plan", lang)
    return (
        user_bubble,
        _msg_bubble("ai", f"You've used all {limit} included AI queries."),
        Div(
            P(t("msg.upgrade_to_keep_your_ai_operator_working", lang), cls="ai-upgrade-label"),
            A(upgrade_label,
              href=subscribe_url("ai"),
              target="_blank", cls="btn btn--primary"),
            cls="ai-upgrade-cta",
        ),
    )


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------

def setup_ui_routes(app) -> None:

    @app.get("/ai")
    async def ai_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        from celerp.services.permissions import role_has_permission
        try:
            settings = (await api.get_company(token)).get("settings") or {}
        except APIError:
            settings = {}
        if not role_has_permission(settings, get_role(request), "use_ai_assistant"):
            return RedirectResponse("/dashboard", status_code=302)

        # Check if AI is available by querying the API (which has the gateway state)
        try:
            status = await api.ai_quota_status(token)
            has_cloud = (
                isinstance(status, dict)
                and not status.get("local")
                and not status.get("unknown")
                and not status.get("disconnected")
                and status.get("tier") in ("cloud", "ai", "team")
            )
        except Exception:
            has_cloud = False

        lang = get_lang(request)
        if not has_cloud:
            content = _showcase_view(lang=lang)
            return await base_shell(
                content,
                title="AI Assistant - Celerp",
                nav_active="ai",
                request=request,
            )

        # A conversation id in the URL opens that thread; an unknown or foreign
        # id falls back to the empty state rather than surfacing an error.
        conversation_id = (request.query_params.get("conversation") or "").strip()
        messages = None
        if conversation_id:
            from celerp.gateway.state import get_session_token
            session_token = get_session_token()
            try:
                conv = await api.ai_conversation_get(token, session_token, conversation_id)
                messages = conv.get("messages") or []
            except APIError:
                conversation_id = ""
                messages = None

        return await base_shell(
            _chat_view(messages=messages, conversation_id=conversation_id, lang=lang),
            title="AI Assistant - Celerp",
            nav_active="ai",
            request=request,
        )

    @app.get("/ai/settings")
    async def ai_settings_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return await base_shell(
            Div(
                id="ai-settings-content",
                hx_get="/ai/settings-content",
                hx_trigger="load",
                hx_swap="innerHTML",
            ),
            title="AI Settings - Celerp",
            nav_active="ai",
            request=request,
        )

    @app.get("/ai/settings-content")
    async def ai_settings_content(request: Request):
        token = _token(request)
        if not token:
            return Div(t("msg.not_authenticated"))

        from celerp.gateway.state import get_session_token
        session_token = get_session_token()

        # Quota section
        quota_section = await _quota_section(token, session_token)

        # Per-user usage table (local DB, no session token needed)
        usage_table = await _usage_table(token, session_token)

        return Div(
            H2(t("page.ai_settings"), cls="ai-settings__title"),
            quota_section,
            usage_table,
            cls="ai-settings",
        )

    @app.post("/ai/chat")
    async def ai_chat(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})

        lang = get_lang(request)
        form = await request.form()
        query = (form.get("query") or "").strip()
        conversation_id = (form.get("conversation_id") or "").strip()
        file_ids_str = (form.get("file_ids") or "").strip()
        file_ids = [fid.strip() for fid in file_ids_str.split(",")] if file_ids_str else None

        if not query and not file_ids:
            return _msg_bubble("ai", t("ai.chat_prompt_empty", lang))

        user_bubble = _msg_bubble("user", query or t("ai.attached_files", lang))

        from celerp.gateway.state import get_session_token
        session_token = get_session_token()

        # An empty conversation id means this is the thread's first message: start
        # the conversation, then run the query inside it.
        if not conversation_id:
            try:
                conversation_id = (await api.ai_conversation_create(token, session_token))["id"]
            except APIError as e:
                return _failed_reply(user_bubble, e, lang)

        try:
            result = await api.ai_conversation_query(
                token, session_token, conversation_id, query, file_ids=file_ids,
            )
        except APIError as e:
            if e.status == 402:
                card_detail = e.detail if isinstance(e.detail, dict) else {}
                return _quota_exceeded_card(card_detail, user_bubble, lang)
            return _failed_reply(user_bubble, e, lang)

        answer = result.get("answer", "")
        cards = [
            _action_card(conversation_id, str(action.get("message_id", "")), action, lang)
            for action in (result.get("pending_actions") or [])
        ]
        # Out-of-band swaps keep the hidden id and the sidebar list in step with
        # the (possibly newly created) conversation; HX-Push-Url puts the thread
        # in the address bar so a reload reopens it.
        oob_id = Input(type="hidden", name="conversation_id", id="ai-conversation-id",
                       value=conversation_id, hx_swap_oob="true")
        oob_history = Div(
            id="ai-history", hx_swap_oob="true",
            hx_get="/ai/conversations-list", hx_trigger="load", hx_swap="innerHTML",
        )
        html = to_xml((user_bubble, _msg_bubble("ai", answer), *cards, oob_id, oob_history))
        return HTMLResponse(html, headers={"HX-Push-Url": f"/ai?conversation={conversation_id}"})

    @app.post("/ai/conversations")
    async def ai_conversation_new(request: Request):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        from celerp.gateway.state import get_session_token
        session_token = get_session_token()
        try:
            conv = await api.ai_conversation_create(token, session_token)
        except APIError as e:
            return _R("", status_code=e.status or 502)
        return _R("", headers={"HX-Redirect": f"/ai?conversation={conv['id']}"})

    @app.post("/ai/confirm-action-ui")
    async def ai_confirm_action_ui(request: Request):
        token = _token(request)
        lang = get_lang(request)
        if not token:
            return _action_panel(t("msg.not_authenticated", lang), ok=False)
        form = await request.form()
        conversation_id = (form.get("conversation_id") or "").strip()
        message_id = (form.get("message_id") or "").strip()
        tool_call_id = (form.get("tool_call_id") or "").strip()

        from celerp.gateway.state import get_session_token
        session_token = get_session_token()
        try:
            result = await api.ai_confirm_action(
                token, session_token, conversation_id, message_id, tool_call_id,
            )
        except APIError as e:
            code = e.detail.get("code") if isinstance(e.detail, dict) else ""
            if e.status == 409 and code == "action_not_pending":
                return _action_panel(t("ai.action_expired", lang), ok=False)
            return _action_panel(f"{t('ai.error_prefix', lang)} {e.detail}", ok=False)

        if result.get("ok"):
            return _action_panel(_confirm_success_text(result.get("data"), lang), ok=True)
        error = result.get("error") or {}
        message = error.get("message") or error.get("code") if isinstance(error, dict) else str(error)
        return _action_panel(f"{t('ai.error_prefix', lang)} {message}", ok=False)

    @app.get("/ai/conversations-list")
    async def ai_conversations_list(request: Request):
        token = _token(request)
        if not token:
            return Div()
        try:
            from celerp.gateway.state import get_session_token
            session_token = get_session_token()
            result = await api.ai_conversations_list(token, session_token)
        except Exception:
            result = []

        if not result:
            return P(t("msg.no_conversations_yet"), cls="ai-sidebar__empty")

        items = []
        for c in result:
            title = c.get("title") or "New conversation"
            items.append(
                A(
                    title,
                    href=f"/ai?conversation={c['id']}",
                    cls="ai-sidebar__item",
                )
            )
        return Div(*items)

    @app.get("/ai/memory-panel")
    async def ai_memory_panel(request: Request):
        token = _token(request)
        if not token:
            return Div()
        try:
            from celerp.gateway.state import get_session_token
            session_token = get_session_token()
            result = await api.ai_memory_get(token, session_token)
        except Exception:
            result = {"notes": [], "kv": {}}

        notes = result.get("notes", [])
        kv = result.get("kv", {})

        note_items = []
        if notes:
            for note in notes:
                content = note.get("content", "") if isinstance(note, dict) else str(note)
                note_items.append(Div(content, cls="ai-memory__note"))
        else:
            note_items.append(P(t("msg.no_notes_saved"), cls="ai-memory__empty"))

        kv_items = []
        if kv:
            for k, v in kv.items():
                kv_items.append(Div(
                    Span(k, cls="ai-memory__kv-key"), Span(str(v)),
                    cls="ai-memory__kv",
                ))
        else:
            kv_items.append(P(t("msg.no_facts_saved"), cls="ai-memory__empty"))

        return Div(
            Div(H4(t("th.notes")), *note_items, cls="ai-memory__section"),
            Div(H4(t("page.facts")), *kv_items, cls="ai-memory__section"),
            Div(
                Button(t("btn.clear_all_memory"), cls="btn btn--danger btn--sm",
                    hx_delete="/ai/memory-clear",
                    hx_target="#ai-memory-content",
                    hx_confirm=t("ai.clear_memory_confirm"),
                ),
                cls="ai-memory__actions",
            ),
            cls="ai-memory",
        )

    @app.delete("/ai/memory-clear")
    async def ai_memory_clear(request: Request):
        token = _token(request)
        if not token:
            return Div()
        try:
            from celerp.gateway.state import get_session_token
            session_token = get_session_token()
            await api.ai_memory_clear(token, session_token)
        except Exception:
            pass
        return P(t("msg.memory_cleared"), cls="ai-memory__empty")

    @app.post("/ai/upload")
    async def ai_upload_proxy(request: Request):
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        form = await request.form()
        files_raw = form.getlist("files")
        if not files_raw:
            return JSONResponse({"detail": "No files"}, status_code=400)

        from celerp.gateway.state import get_session_token
        session_token = get_session_token()

        files = []
        for f in files_raw:
            content = await f.read()
            files.append((f.filename, content, f.content_type or "application/octet-stream"))

        try:
            result = await api.ai_upload(token, session_token, files)
            return JSONResponse(result, status_code=201)
        except APIError as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status)

    @app.get("/ai/quota-status")
    async def ai_quota_status_proxy(request: Request):
        from starlette.responses import JSONResponse
        token = _token(request)
        if not token:
            return JSONResponse({"local": True})
        try:
            from celerp.gateway.state import get_session_token
            session_token = get_session_token()
            result = await api.ai_quota_status(token, session_token)
            # Compute the top-up destination server-side through the commercial
            # policy so the client never builds a direct checkout URL itself.
            if isinstance(result, dict):
                result = {**result, "topup_url": topup_url()}
            return JSONResponse(result)
        except Exception:
            return JSONResponse({"local": True})


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

async def _quota_section(token: str, session_token: str) -> FT:
    """Render quota overview. Falls back to local-install message if no relay."""
    try:
        status = await api.ai_quota_status(token, session_token)
    except Exception:
        return Div(
            H3(t("page.quota"), cls="ai-settings__section-title"),
            P(t("msg.could_not_load_quota_data"), cls="ai-settings__error"),
            cls="ai-settings__quota",
        )

    if status.get("local"):
        return Div(
            H3(t("page.quota"), cls="ai-settings__section-title"),
            P(t("msg.local_install_no_usage_limits"), cls="ai-settings__local-msg"),
            cls="ai-settings__quota",
        )

    used = int(status.get("used", 0) or 0)
    limit = int(status.get("base_limit", status.get("limit", 0)) or 0)
    topup = int(status.get("topup_balance", status.get("topup_credits", 0)) or 0)
    remaining = int(status.get("remaining", 0) or 0)
    resets_at = status.get("resets_at", "")
    pct = min(100, round(used / limit * 100)) if limit else 0

    return Div(
        H3(t("page.quota"), cls="ai-settings__section-title"),
        Div(
            Div(
                Span(t("msg.monthly_limit"), cls="ai-settings__stat-label"),
                Span(str(limit), cls="ai-settings__stat-value"),
                cls="ai-settings__stat",
            ),
            Div(
                Span(t("msg.used_this_period"), cls="ai-settings__stat-label"),
                Span(str(used), cls="ai-settings__stat-value"),
                cls="ai-settings__stat",
            ),
            Div(
                Span(t("ai.top_up_credits"), cls="ai-settings__stat-label"),
                Span(str(topup), cls="ai-settings__stat-value"),
                cls="ai-settings__stat",
            ),
            Div(
                Span(t("msg.remaining"), cls="ai-settings__stat-label"),
                Span(str(remaining), cls="ai-settings__stat-value ai-settings__stat-value--highlight"),
                cls="ai-settings__stat",
            ),
            cls="ai-settings__stats",
        ),
        Div(
            Div(cls="ai-settings__progress-fill", style=f"width:{pct}%"),
            cls="ai-settings__progress",
        ),
        Div(
            Span(f"Resets {resets_at}" if resets_at else "", cls="ai-settings__reset-date"),
            A(t("msg.buy_more_credits"), href=topup_url(), target="_blank",
              cls="ai-settings__buy-link"),
            cls="ai-settings__quota-footer",
        ),
        cls="ai-settings__quota",
    )


async def _usage_table(token: str, session_token: str) -> FT:
    """Render per-user usage table for current month."""
    try:
        data = await api.ai_usage_stats(token, session_token)
        rows = data.get("users", [])
    except Exception:
        return Div(
            H3(t("page.this_months_usage"), cls="ai-settings__section-title"),
            P(t("msg.could_not_load_usage_data"), cls="ai-settings__error"),
        )

    if not rows:
        return Div(
            H3(t("page.this_months_usage"), cls="ai-settings__section-title"),
            P(t("msg.no_queries_this_month_yet"), cls="ai-settings__empty"),
        )

    table_rows = []
    for r in rows:
        last_q = r.get("last_query_at") or ""
        if last_q:
            # Show date only
            last_q = last_q[:10]
        table_rows.append(Tr(
            Td(r.get("user_name", "—"), cls="td-left"),
            Td(str(r.get("query_count", 0)), cls="td-right"),
            Td(str(r.get("credits_used", 0)), cls="td-right"),
            Td(last_q, cls="td-right"),
        ))

    return Div(
        H3(t("page.this_months_usage"), cls="ai-settings__section-title"),
        Table(
            Thead(Tr(
                Th(t("th.user"), cls="th-center"),
                Th(t("th.queries"), cls="th-center"),
                Th(t("th.credits_used"), cls="th-center"),
                Th(t("th.last_query"), cls="th-center"),
            )),
            Tbody(*table_rows),
            cls="table ai-settings__usage-table",
        ),
        cls="ai-settings__usage",
    )


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def _msg_bubble(role: str, text: str) -> FT:
    cls = "ai-msg ai-msg--user" if role == "user" else "ai-msg ai-msg--ai"
    return Div(text, cls=cls)


def _failed_reply(user_bubble: FT, e: APIError, lang: str) -> tuple[FT, FT]:
    """The user's bubble followed by the assistant's note that the request failed.

    Returned as a fragment, never wrapped: each bubble must be a direct child of
    the message column so it takes the same alignment as a reloaded thread.
    """
    if e.status == 429:
        return user_bubble, _msg_bubble("ai", t("ai.busy", lang))
    return user_bubble, _msg_bubble("ai", f"{t('ai.error_prefix', lang)} {e.detail}")


def _arg_lines(section: dict) -> FT:
    """A definition-style list of one argument group (body, path, or query)."""
    return Div(
        *[
            Div(
                Span(str(key), cls="ai-action__line-key"),
                Span(str(value), cls="ai-action__line-val"),
                cls="ai-action__line",
            )
            for key, value in section.items()
        ],
        cls="ai-action__lines",
    )


def _action_card(conversation_id: str, message_id: str, action: dict, lang: str = "en") -> FT:
    """Render one pending mutation the user must confirm before it runs.

    The arguments are shown read-only, grouped by request part; Confirm posts the
    identifiers only (never the arguments) so the server executes the action it
    claimed, and Dismiss removes the card client-side without touching state.
    """
    name = action.get("name", "")
    tool_call_id = action.get("id", "")
    args = action.get("arguments") or {}
    groups = [
        _arg_lines(args[part])
        for part in ("body", "path", "query")
        if isinstance(args.get(part), dict) and args[part]
    ]
    return Div(
        Div(
            P(t("ai.action_proposal", lang), cls="ai-action__title"),
            Span(name, cls="ai-action__meta"),
            cls="ai-action__header",
        ),
        Div(*groups, cls="ai-action__list"),
        Div(
            Form(
                Input(type="hidden", name="conversation_id", value=conversation_id),
                Input(type="hidden", name="message_id", value=message_id),
                Input(type="hidden", name="tool_call_id", value=tool_call_id),
                Button(t("btn.confirm", lang), type="submit", cls="btn btn--primary"),
                hx_post="/ai/confirm-action-ui",
                hx_target="closest .ai-action__card",
                hx_swap="outerHTML",
            ),
            Button(t("btn.dismiss", lang), cls="btn btn--secondary",
                   onclick="this.closest('.ai-action__card').remove()"),
            cls="ai-action__actions",
        ),
        cls="ai-action ai-action__card",
    )


def _confirm_success_text(data, lang: str = "en") -> str:
    """Success line after a confirmed action, naming the entity when the route
    returns one so the user sees exactly what was created or changed."""
    done = t("ai.action_done", lang)
    if isinstance(data, dict):
        name = data.get("name") or data.get("sku") or data.get("title")
        ident = data.get("id") or data.get("entity_id")
        if name and ident:
            return f"{done} {name} (#{ident})"
        if name:
            return f"{done} {name}"
    return done


def _action_panel(text: str, *, ok: bool) -> FT:
    """The card is replaced by this line once an action is confirmed or fails."""
    cls = "ai-action__done" if ok else "ai-action__error"
    return Div(text, cls=cls)


def _empty_state(lang: str = "en") -> FT:
    """Chat empty state with example query cards."""
    cards = [
        Div(
            Span(q["icon"], cls="ai-empty-state__card-icon"),
            Div(
                Strong(q["title"], cls="ai-empty-state__card-title"),
                P(q["query"], cls="ai-empty-state__card-query"),
                cls="ai-empty-state__card-body",
            ),
            cls="ai-empty-state__card",
            onclick=f"celerpAiFillQuery({json.dumps(q['query'])},{'true' if q.get('needs_files') else 'false'})",
        )
        for q in _get_example_queries(lang)
    ]
    return Div(
        Div(
            P("✨", cls="ai-empty-state__icon"),
            H3(t("page.how_can_i_help"), cls="ai-empty-state__heading"),
            P(t("msg.here_are_some_things_i_can_do"), cls="ai-empty-state__sub"),
            cls="ai-empty-state__welcome",
        ),
        Div(*cards, cls="ai-empty-state__grid"),
        id="ai-empty-state",
        cls="ai-empty-state",
    )


def _showcase_view(lang: str = "en") -> FT:
    scenarios = _get_scenarios(lang)
    scenarios_json = json.dumps(scenarios)
    return Div(
        Div(
            H1(t("page.meet_your_ai_operator"), cls="ai-showcase__headline"),
            P(
                t("ai.showcase_subtitle", lang),
                cls="ai-showcase__sub",
            ),
            Div(
                *[
                    Button(
                        s["label"],
                        cls="ai-showcase__tab",
                        data_scenario=s["id"],
                        onclick="celerpShowcaseSelect(this)",
                    )
                    for s in scenarios
                ],
                cls="ai-showcase__scenarios",
            ),
            Div(
                Div(
                    Span("", cls="ai-showcase__dot ai-showcase__dot--red"),
                    Span("", cls="ai-showcase__dot ai-showcase__dot--yellow"),
                    Span("", cls="ai-showcase__dot ai-showcase__dot--green"),
                    Span(t("msg.celerp_ai"), cls="ai-showcase__term-title"),
                    cls="ai-showcase__term-bar",
                ),
                Div(id="showcase-messages", cls="ai-showcase__messages"),
                Div(
                    Span("📎", cls="ai-showcase__input-icon"),
                    Div(t("msg.ask_anything_about_your_business_data"),
                        cls="ai-showcase__input-field ai-input__field--disabled"),
                    Span(t("btn.send"), cls="ai-showcase__input-send"),
                    cls="ai-showcase__input ai-input--disabled",
                ),
                cls="ai-showcase__terminal",
            ),
            Div(
                P(t("msg.stop_doing_manual_data_entry_hire_an_ai_operator"),
                  cls="ai-showcase__cta-headline"),
                Div(
                    Div(
                        Span(t("msg.start_here"), cls="ai-showcase__cta-badge ai-showcase__cta-badge--default"),
                        P(t("settings.tab_cloud_relay"), cls="ai-showcase__cta-name"),
                        P("", cls="ai-showcase__cta-price ai-showcase__cta-price--default"),
                        P(t("msg.secure_remote_access"), cls="ai-showcase__cta-feature"),
                        P(t("msg.automated_daily_backups"), cls="ai-showcase__cta-feature"),
                        P(t("msg.100_lifetime_ai_queries_included"),
                          cls="ai-showcase__cta-feature ai-showcase__cta-feature--highlight"),
                        Div(
                            A(t("msg.start_with_cloud_relay"),
                              href=subscribe_url("cloud"),
                              target="_blank", cls="btn btn--outline"),
                            P(t("msg.cancel_anytime"), cls="ai-showcase__cta-fine"),
                            cls="ai-showcase__cta-actions",
                        ),
                        cls="ai-showcase__cta-card",
                    ),
                    Div(
                        Span(t("msg.recommended"), cls="ai-showcase__cta-badge ai-showcase__cta-badge--featured"),
                        P(t("msg.celerp_ai_plan"), cls="ai-showcase__cta-name"),
                        P("", cls="ai-showcase__cta-price ai-showcase__cta-price--featured"),
                        P(t("msg.200_ai_queries_every_month"), cls="ai-showcase__cta-feature"),
                        P(t("msg.batch_invoice_pdf_processing"), cls="ai-showcase__cta-feature"),
                        P(t("msg.agentic_record_creation"), cls="ai-showcase__cta-feature"),
                        Div(
                            A(t("msg.get_the_ai_plan"),
                              href=subscribe_url("ai"),
                              target="_blank", cls="btn btn--accent"),
                            P(t("msg.cancel_anytime"), cls="ai-showcase__cta-fine"),
                            cls="ai-showcase__cta-actions",
                        ),
                        cls="ai-showcase__cta-card ai-showcase__cta-card--featured",
                    ),
                    cls="ai-showcase__cta-cards",
                ),
                *([] if is_partner_managed() else [
                    Div(
                        P(t("page.already_subscribed", lang),
                          cls="ai-showcase__restore-label"),
                        A(t("btn.link_by_email", lang), href="/settings/cloud",
                          cls="btn btn--outline"),
                        cls="ai-showcase__restore",
                    )
                ]),
                cls="ai-showcase__cta",
            ),
            cls="ai-showcase",
            data_scenarios=scenarios_json,
        ),
        _showcase_script(),
        cls="ai-page",
    )


def _thread(messages: list[dict], conversation_id: str, lang: str) -> list[FT]:
    """Render a stored conversation into message bubbles, with one action card
    per still-open pending action beneath the assistant turn that proposed it."""
    out: list[FT] = []
    for m in messages:
        role = m.get("role")
        out.append(_msg_bubble("user" if role == "user" else "ai", m.get("content") or ""))
        for action in (m.get("pending_actions") or []):
            out.append(_action_card(conversation_id, str(m.get("id", "")), action, lang))
    return out


def _chat_view(messages: list[dict] | None = None, conversation_id: str = "", lang: str = "en") -> FT:
    if messages:
        message_children = _thread(messages, conversation_id, lang)
    else:
        message_children = [_empty_state(lang)]
    return Div(
        # Sidebar
        Div(
            # Collapse toggle
            Button(
                "☰",
                cls="ai-sidebar__toggle",
                id="ai-sidebar-toggle",
                title=t("ai.toggle_sidebar", lang),
                onclick="celerpAiToggleSidebar()",
            ),
            # Expanded action buttons (stacked vertically)
            Div(
                Button(t("btn._new", lang),
                    cls="btn btn--secondary ai-sidebar__new",
                    hx_post="/ai/conversations",
                ),
                Button(t("btn._memory", lang),
                    cls="btn btn--secondary ai-sidebar__memory-btn",
                    style="width:100%;text-align:left;",
                    hx_get="/ai/memory-panel",
                    hx_target="#ai-memory-content",
                    hx_swap="innerHTML",
                    onclick="document.getElementById('ai-memory-drawer').classList.toggle('ai-memory-drawer--open')",
                ),
                cls="ai-sidebar__actions",
            ),
            # Collapsed icon buttons
            Div(
                Button(
                    "+",
                    cls="ai-sidebar__icon-btn",
                    title=t("ai.new_conversation", lang),
                    hx_post="/ai/conversations",
                ),
                Button(
                    "🧠",
                    cls="ai-sidebar__icon-btn",
                    title=t("ai.memory", lang),
                    hx_get="/ai/memory-panel",
                    hx_target="#ai-memory-content",
                    hx_swap="innerHTML",
                    onclick="document.getElementById('ai-memory-drawer').classList.toggle('ai-memory-drawer--open')",
                ),
                cls="ai-sidebar__actions-icon",
            ),
            Div(
                id="ai-history",
                hx_get="/ai/conversations-list",
                hx_trigger="load",
                hx_swap="innerHTML",
            ),
            Div(
                Div(id="ai-memory-content"),
                id="ai-memory-drawer",
                cls="ai-memory-drawer",
            ),
            id="ai-sidebar",
            cls="ai-sidebar",
        ),
        # Main chat area
        Div(
            Div(
                Span(id="ai-quota-display", cls="ai-quota"),
                A(t("msg.buy_more_credits", lang), id="ai-topup-link", href="#",
                  target="_blank", cls="ai-topup-link", style="display:none;"),
                cls="ai-chat__header",
            ),
            # Messages + empty state
            Div(
                *message_children,
                id="ai-messages",
                cls="ai-messages",
            ),
            # Input area: text row + drop zone
            Form(
                Input(
                    type="file",
                    id="ai-file-input",
                    multiple=True,
                    style="display: none;",
                    accept="image/jpeg,image/png,image/gif,image/webp,application/pdf,.csv,.xlsx",
                    onchange="celerpAiHandleFiles(this)",
                ),
                Input(
                    type="hidden",
                    name="file_ids",
                    id="ai-file-ids",
                    value="",
                ),
                Input(
                    type="hidden",
                    name="conversation_id",
                    id="ai-conversation-id",
                    value=conversation_id,
                ),
                # Text input row
                Div(
                    Input(
                        type="text",
                        name="query",
                        id="ai-query-input",
                        placeholder=t("msg.ask_anything_about_your_business_data", lang),
                        cls="ai-input__field",
                        autocomplete="off",
                    ),
                    Button(t("btn.send", lang), type="submit", cls="btn btn--primary ai-input__send"),
                    cls="ai-input__row",
                ),
                # Drop zone
                Div(
                    Span("📁", cls="ai-chat-dropzone__icon"),
                    Span(t("msg.drop_files_here_or_click_to_browse", lang), cls="ai-chat-dropzone__label"),
                    Div(id="ai-file-chips", cls="ai-file-chips"),
                    id="ai-chat-dropzone",
                    cls="ai-chat-dropzone",
                    onclick="document.getElementById('ai-file-input').click()",
                ),
                hx_post="/ai/chat",
                hx_target="#ai-messages",
                hx_swap="beforeend",
                hx_on__before_request="celerpAiBeforeRequest();",
                hx_on__after_request="celerpAiResetForm();",
                cls="ai-input",
                id="ai-chat-form",
            ),
            Script(_chat_script()),
            cls="ai-chat__main",
        ),
        cls="ai-chat",
    )


def _chat_script() -> str:
    return r"""
// ── Sidebar collapse ─────────────────────────────────────────────────────────
(function() {
    var sidebar = document.getElementById('ai-sidebar');
    if (!sidebar) return;
    var collapsed = localStorage.getItem('celerp_ai_sidebar_collapsed') === '1';
    if (collapsed) sidebar.classList.add('ai-sidebar--collapsed');
    // Mobile: hidden by default
    if (window.innerWidth < 640 && !sidebar.classList.contains('ai-sidebar--mobile-open')) {
        sidebar.classList.add('ai-sidebar--collapsed');
    }
})();

function celerpAiToggleSidebar() {
    var sidebar = document.getElementById('ai-sidebar');
    if (!sidebar) return;
    if (window.innerWidth < 640) {
        sidebar.classList.toggle('ai-sidebar--mobile-open');
    } else {
        var collapsed = sidebar.classList.toggle('ai-sidebar--collapsed');
        localStorage.setItem('celerp_ai_sidebar_collapsed', collapsed ? '1' : '0');
    }
}

function celerpAiBeforeRequest() {
    // Hide empty state on first message
    var es = document.getElementById('ai-empty-state');
    if (es) es.style.display = 'none';

    var m = document.getElementById('ai-messages');
    var typing = document.createElement('div');
    typing.id = 'ai-typing-indicator';
    typing.className = 'ai-msg ai-msg--ai';
    typing.innerHTML = '<div class="ai-typing"><span class="ai-typing__dot"></span><span class="ai-typing__dot"></span><span class="ai-typing__dot"></span></div>';
    m.appendChild(typing);
    m.scrollTop = m.scrollHeight;
}

function celerpAiFillQuery(text, needsFiles) {
    if (needsFiles) {
        var fids = document.getElementById('ai-file-ids').value;
        if (!fids) {
            // Highlight the drop zone and scroll to it
            var zone = document.getElementById('ai-chat-dropzone');
            if (zone) {
                zone.classList.add('ai-chat-dropzone--active');
                zone.scrollIntoView({behavior: 'smooth', block: 'nearest'});
                setTimeout(function() { zone.classList.remove('ai-chat-dropzone--active'); }, 2000);
            }
            var inp = document.getElementById('ai-query-input');
            inp.value = text;
            inp.focus();
            return;
        }
    }
    var inp = document.getElementById('ai-query-input');
    inp.value = text;
    inp.focus();
}

function celerpAiHandleFiles(input) {
    if (!input.files || input.files.length === 0) return;
    var formData = new FormData();
    var fileNames = [];
    for (var i = 0; i < input.files.length; i++) {
        formData.append('files', input.files[i]);
        fileNames.push(input.files[i].name);
    }
    _celerpAiUploadFormData(formData, fileNames);
}

function celerpAiRemoveChip(btn, fileId) {
    var chip = btn.closest('.ai-file-chip');
    if (chip) chip.remove();
    var fidsInput = document.getElementById('ai-file-ids');
    var ids = fidsInput.value ? fidsInput.value.split(',').filter(function(id) { return id !== fileId; }) : [];
    fidsInput.value = ids.join(',');
    var chips = document.getElementById('ai-file-chips');
    if (!chips.children.length) {
        document.getElementById('ai-chat-dropzone').classList.remove('ai-chat-dropzone--has-files');
    }
}

function celerpAiResetForm() {
    var typing = document.getElementById('ai-typing-indicator');
    if (typing) typing.remove();

    document.getElementById('ai-query-input').value = '';
    document.getElementById('ai-file-ids').value = '';
    document.getElementById('ai-file-input').value = '';
    document.getElementById('ai-file-chips').innerHTML = '';
    document.getElementById('ai-chat-dropzone').classList.remove('ai-chat-dropzone--has-files');

    var m = document.getElementById('ai-messages');
    m.scrollTop = m.scrollHeight;
}

// Drag-and-drop handlers on the drop zone
(function() {
    var zone = document.getElementById('ai-chat-dropzone');
    if (!zone) return;

    zone.addEventListener('dragover', function(e) {
        e.preventDefault();
        zone.classList.add('ai-chat-dropzone--active');
    });
    zone.addEventListener('dragenter', function(e) {
        e.preventDefault();
        zone.classList.add('ai-chat-dropzone--active');
    });
    zone.addEventListener('dragleave', function() {
        zone.classList.remove('ai-chat-dropzone--active');
    });
    zone.addEventListener('drop', function(e) {
        e.preventDefault();
        zone.classList.remove('ai-chat-dropzone--active');
        var dt = e.dataTransfer;
        if (dt && dt.files && dt.files.length) {
            // Trigger upload with the dropped files
            var fakeInput = document.getElementById('ai-file-input');
            // Build FormData directly from dropped files
            var formData = new FormData();
            var fileNames = [];
            for (var i = 0; i < dt.files.length; i++) {
                formData.append('files', dt.files[i]);
                fileNames.push(dt.files[i].name);
            }
            _celerpAiUploadFormData(formData, fileNames);
        }
    });
})();

function _celerpAiUploadFormData(formData, fileNames) {
    var zone = document.getElementById('ai-chat-dropzone');
    var chips = document.getElementById('ai-file-chips');

    var progressWrap = document.getElementById('ai-upload-progress');
    if (!progressWrap) {
        progressWrap = document.createElement('div');
        progressWrap.id = 'ai-upload-progress';
        progressWrap.className = 'ai-upload-progress';
        progressWrap.innerHTML = '<div class="ai-upload-progress__bar"></div><span class="ai-upload-progress__text">Uploading...</span>';
        zone.appendChild(progressWrap);
    }
    progressWrap.style.display = 'flex';
    var bar = progressWrap.querySelector('.ai-upload-progress__bar');
    var ptext = progressWrap.querySelector('.ai-upload-progress__text');
    bar.style.width = '0%';

    var xhr = new XMLHttpRequest();
    xhr.upload.addEventListener('progress', function(e) {
        if (e.lengthComputable) {
            var pct = Math.round((e.loaded / e.total) * 100);
            bar.style.width = pct + '%';
            ptext.textContent = pct + '%';
        }
    });
    xhr.addEventListener('load', function() {
        progressWrap.style.display = 'none';
        if (xhr.status >= 200 && xhr.status < 300) {
            var data = JSON.parse(xhr.responseText);
            if (data.file_ids) {
                var existing = document.getElementById('ai-file-ids').value;
                var all = existing ? existing.split(',').concat(data.file_ids) : data.file_ids;
                document.getElementById('ai-file-ids').value = all.join(',');
                zone.classList.add('ai-chat-dropzone--has-files');
                data.file_ids.forEach(function(fid, idx) {
                    var fname = fileNames[idx] || fid;
                    var chip = document.createElement('span');
                    chip.className = 'ai-file-chip';
                    chip.dataset.fileId = fid;
                    chip.innerHTML = '<span class="ai-file-chip__name">' + fname + '</span>'
                        + '<button type="button" class="ai-file-chip__remove" onclick="celerpAiRemoveChip(this,' + JSON.stringify(fid) + ')">✕</button>';
                    chips.appendChild(chip);
                });
            }
        }
    });
    xhr.addEventListener('error', function() {
        progressWrap.style.display = 'none';
    });
    xhr.open('POST', '/ai/upload');
    xhr.send(formData);
}

// Load quota status on page init
(function() {
    fetch('/ai/quota-status')
    .then(function(r) { return r.json(); })
    .then(function(data) {
        var badge = document.getElementById('ai-quota-display');
        var link = document.getElementById('ai-topup-link');
        if (data.local) { badge.textContent = ''; return; }
        var remaining = data.remaining || 0;
        badge.textContent = remaining + ' credits remaining';
        if (remaining < 10) {
            badge.classList.add('ai-quota--low');
        }
        if (remaining < 20 && (data.tier === 'ai' || data.tier === 'team') && data.topup_url) {
            link.href = data.topup_url;
            link.style.display = 'inline';
        }
    })
    .catch(function() {});
})();
"""


def _showcase_script() -> FT:
    js = r"""
(function() {
  var container = document.querySelector('[data-scenarios]');
  if (!container) return;
  var scenarios = JSON.parse(container.dataset.scenarios);
  var msgArea = document.getElementById('showcase-messages');
  var tabs = document.querySelectorAll('.ai-showcase__tab');
  var current = 0, timer = null, paused = false, typingIv = null;

  var terminal = document.querySelector('.ai-showcase__terminal');
  if (terminal) {
    terminal.addEventListener('mouseenter', function() { paused = true; });
    terminal.addEventListener('mouseleave', function() { paused = false; });
  }
  var scenarioBar = document.querySelector('.ai-showcase__scenarios');
  if (scenarioBar) {
    scenarioBar.addEventListener('mouseenter', function() { paused = true; });
    scenarioBar.addEventListener('mouseleave', function() { paused = false; });
  }

  function clear() { msgArea.innerHTML = ''; if (typingIv) { clearInterval(typingIv); typingIv = null; } }

  function appendMsg(role, text, cb) {
    var el = document.createElement('div');
    el.className = 'ai-msg ' + (role === 'user' ? 'ai-msg--user' : 'ai-msg--ai');
    msgArea.appendChild(el);
    msgArea.scrollTop = msgArea.scrollHeight;
    if (role === 'user') { el.textContent = text; if (cb) cb(); return; }
    var i = 0;
    typingIv = setInterval(function() {
      el.textContent = text.slice(0, i++);
      msgArea.scrollTop = msgArea.scrollHeight;
      if (i > text.length) { clearInterval(typingIv); typingIv = null; if (cb) cb(); }
    }, 14);
  }

  function appendThinking(text, cb) {
    var el = document.createElement('div');
    el.className = 'ai-msg ai-msg--thinking';
    el.textContent = text;
    msgArea.appendChild(el);
    msgArea.scrollTop = msgArea.scrollHeight;
    setTimeout(function() { el.remove(); if (cb) cb(); }, 1200);
  }

  function scheduleNext(idx) {
    var elapsed = 0;
    var check = setInterval(function() {
      if (!paused) elapsed += 100;
      if (elapsed >= 4000) {
        clearInterval(check);
        current = (idx + 1) % scenarios.length;
        playScenario(current);
      }
    }, 100);
    timer = check;
  }

  function playScenario(idx) {
    if (timer) clearInterval(timer);
    clear();
    tabs.forEach(function(t, i) { t.classList.toggle('ai-showcase__tab--active', i === idx); });
    var s = scenarios[idx];
    appendMsg('user', s.user, function() {
      setTimeout(function() {
        appendThinking(s.thinking, function() {
          appendMsg('ai', s.reply, function() {
            scheduleNext(idx);
          });
        });
      }, 400);
    });
  }

  window.celerpShowcaseSelect = function(btn) {
    var id = btn.dataset.scenario;
    var idx = scenarios.findIndex(function(s) { return s.id === id; });
    if (idx >= 0) { current = idx; playScenario(idx); }
  };

  // A checkout opens in a new tab. When the user returns, re-use the existing
  // quota/entitlement endpoint to converge the already-open showcase into the
  // paid chat view. Keep checks bounded and only arm them after a purchase CTA
  // was actually clicked, so ordinary free users do not poll the relay.
  var purchasePending = false;
  var entitlementCheck = null;
  var entitlementRetry = null;

  document.querySelectorAll('.ai-showcase__cta a').forEach(function(a) {
    a.addEventListener('click', function() { purchasePending = true; });
  });

  function paidEntitlement(data) {
    return data && !data.local && !data.unknown && !data.disconnected &&
      ['cloud', 'ai', 'team'].indexOf(data.tier) >= 0;
  }

  function retryEntitlement(attempts) {
    if (attempts <= 1 || !purchasePending || entitlementRetry) return;
    entitlementRetry = setTimeout(function() {
      entitlementRetry = null;
      checkEntitlement(attempts - 1);
    }, 1500);
  }

  function checkEntitlement(attempts) {
    if (!purchasePending || entitlementCheck) return;
    entitlementCheck = fetch('/ai/quota-status', {cache: 'no-store'})
      .then(function(r) {
        if (!r.ok) throw new Error('quota status failed');
        return r.json();
      })
      .then(function(data) {
        if (paidEntitlement(data)) {
          purchasePending = false;
          window.location.reload();
          return;
        }
        retryEntitlement(attempts);
      })
      .catch(function() { retryEntitlement(attempts); })
      .finally(function() { entitlementCheck = null; });
  }

  function resumeEntitlementCheck() {
    if (purchasePending && !entitlementRetry) checkEntitlement(10);
  }

  window.addEventListener('focus', resumeEntitlementCheck);
  document.addEventListener('visibilitychange', function() {
    if (!document.hidden) resumeEntitlementCheck();
  });

  playScenario(0);
})();
"""
    return Script(js)
