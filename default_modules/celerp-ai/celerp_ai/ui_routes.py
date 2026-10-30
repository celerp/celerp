# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import json
import uuid

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from celerp.gateway.state import PAID_TIERS
import ui.api_client as api
from ui.api_client import APIError
from ui.components.activity import QTY_FIELD_KEYS, fmt_price, fmt_qty, is_money_field
from ui.components.cloud_gate import (
    subscribe_url,
    topup_url,
    is_partner_managed,
)
from ui.components.shell import base_shell, load_error
from ui.config import get_token as _token, get_role
from ui.i18n import refusal_text, t, get_lang
from ui.security import not_permitted_redirect

# A proposal set larger than this renders as the review table instead of cards.
TABLE_FROM = 5
# Actions run per request of a bulk confirm; the tail form chains the rest.
CONFIRM_CHUNK = 10


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
            "id": "reconcile",
            "label": t("ai.scenario_reconcile_label", lang),
            "user": t("ai.scenario_reconcile_user", lang),
            "thinking": t("ai.scenario_reconcile_thinking", lang),
            "reply": t("ai.scenario_reconcile_reply", lang),
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

def _credits_exhausted_text(detail: dict, lang: str) -> str:
    return t("ai.credits_exhausted", lang, limit=int(detail.get("base_limit", detail.get("limit", 0)) or 0))


def _quota_exceeded_card(detail: dict, lang: str = "en") -> tuple[FT, FT]:
    """The card shown when an AI query is refused for hitting the quota.

    Routes both destinations through the commercial policy: the top-up CTA via
    ``topup_url()`` and the upgrade CTA via ``subscribe_url("ai")``. A
    relay-supplied ``detail.upgrade_url`` is never used - the resolver alone
    decides the destination, so a partner-managed install is never sent to a
    direct Celerp checkout. The upgrade label's direct price is suppressed under
    partner_managed.
    """
    is_ai_tier = detail.get("tier") in PAID_TIERS
    if is_ai_tier:
        return (
            _msg_bubble("ai", _credits_exhausted_text(detail, lang)),
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
        _msg_bubble("ai", _credits_exhausted_text(detail, lang)),
        Div(
            P(t("msg.upgrade_to_keep_your_ai_operator_working", lang), cls="ai-upgrade-label"),
            A(upgrade_label,
              href=subscribe_url("ai"),
              target="_blank", cls="btn btn--primary"),
            cls="ai-upgrade-cta",
        ),
    )


def _refusal_text(status: int | None, data: dict, lang: str) -> tuple[str, bool] | None:
    """(text, is_error) for a question the AI service refused, read from its refusal
    code; None when the refusal is not one of the AI service's own. Shared by the live
    reply and a reloaded thread, so both explain a refusal the same way."""
    code = data.get("code")
    if status == 402 and code == "quota_exceeded" and data.get("tier") == "free":
        return t("ai.free_used_up", lang, limit=int(data.get("base_limit") or 0)), False
    if status == 402 and code == "not_available":
        return t("ai.free_not_eligible", lang), True
    if status == 402 and code == "plan_limit":
        return refusal_text(data) or t("error.unexpected_error_body", lang), True
    if status == 402:
        return _credits_exhausted_text(data, lang), False
    if status == 503 and code == "free_paused":
        return t("ai.free_paused", lang, date=str(data.get("resets_at") or "")[:10]), False
    if status == 413 and code == "free_text_too_large":
        return t("ai.free_text_too_large", lang), True
    return None


async def _refusal_reply(token: str, e: APIError, user_bubble: FT, query: str, lang: str) -> tuple:
    """The reply to a send the API refused: the AI service's own refusal as
    ``_refused`` explains it, any other error as a failed request."""
    data = e.data if isinstance(e.data, dict) else {}
    if _refusal_text(e.status, data, lang) is None:
        return _failed_reply(user_bubble, e, lang)
    return (user_bubble, *await _refused(token, e.status, data, query, lang))


async def _refused(token: str, status: int | None, data: dict, query: str, lang: str) -> tuple:
    """The assistant's reply to a question the AI service refused, whether the send
    or the question's job met the refusal. The refusal code decides: a free account
    out of questions or paused gets help pages that may answer it, a used-up paid plan
    gets top-up, a free account the upgrade offer, a busy service says so."""
    explained = _refusal_text(status, data, lang)
    if explained is None:
        if status == 429:
            return (_msg_bubble("ai", t("ai.busy", lang)),)
        return (_msg_bubble("ai", str(data.get("message") or data.get("code") or ""), error=True),)
    text, is_error = explained
    bubble = _msg_bubble("ai", text, error=is_error)
    code = data.get("code")
    if status == 402 and code == "quota_exceeded" and data.get("tier") == "free":
        cards = _plan_cards(await _plan_counts(token), lang)
        return (bubble, *await _help_hits(token, query, lang), cards)
    if status == 402 and code not in ("not_available", "plan_limit"):
        return _quota_exceeded_card(data, lang)
    if status == 503 and code == "free_paused":
        return (bubble, *await _help_hits(token, query, lang))
    return (bubble,)


async def _help_hits(token: str, query: str, lang: str) -> list[FT]:
    """Up to three help pages matching the refused question; none when the search fails."""
    if not query:
        return []
    try:
        hits = (await api.help_search(token, query)).get("hits") or []
    except APIError:
        return []
    links = [(str(h["id"]), str(h.get("title") or h["id"])) for h in hits[:3] if h.get("id")]
    if not links:
        return []
    return [P(t("ai.help_hits", lang), " ", *_page_links(links), cls="ai-msg ai-msg--ai ai-msg__sources")]


async def _ai_offer(token: str) -> dict:
    """The AI offer (paid flag and included question counts); empty when it cannot load,
    so every count line it feeds is left out rather than guessed."""
    try:
        return await api.ai_offer(token)
    except APIError:
        return {}


async def _plan_counts(token: str) -> dict:
    return (await _ai_offer(token)).get("plan_ai_questions") or {}


def _plan_cards(counts: dict, lang: str) -> FT:
    """The Celerp Connect and Connect + AI subscribe cards. Each count line comes from
    the relay's included questions per plan and is left out when unknown."""
    def count_line(key: str, tier: str, *extra_cls: str) -> list[FT]:
        n = counts.get(tier)
        if not isinstance(n, int):
            return []
        return [P(t(key, lang, n=n), cls=" ".join(("ai-showcase__cta-feature", *extra_cls)))]

    return Div(
        Div(
            Span(t("msg.start_here", lang), cls="ai-showcase__cta-badge ai-showcase__cta-badge--default"),
            P(t("settings.tab_cloud_relay", lang), cls="ai-showcase__cta-name"),
            P("", cls="ai-showcase__cta-price ai-showcase__cta-price--default"),
            P(t("msg.secure_remote_access", lang), cls="ai-showcase__cta-feature"),
            P(t("msg.automated_daily_backups", lang), cls="ai-showcase__cta-feature"),
            *count_line("ai.plan_lifetime_credits", "cloud", "ai-showcase__cta-feature--highlight"),
            Div(
                A(t("msg.start_with_cloud_relay", lang),
                  href=subscribe_url("cloud"),
                  target="_blank", cls="btn btn--outline"),
                P(t("msg.cancel_anytime", lang), cls="ai-showcase__cta-fine"),
                cls="ai-showcase__cta-actions",
            ),
            cls="ai-showcase__cta-card",
        ),
        Div(
            Span(t("msg.recommended", lang), cls="ai-showcase__cta-badge ai-showcase__cta-badge--featured"),
            P(t("msg.celerp_ai_plan", lang), cls="ai-showcase__cta-name"),
            P("", cls="ai-showcase__cta-price ai-showcase__cta-price--featured"),
            *count_line("ai.plan_monthly_credits", "ai"),
            P(t("msg.batch_invoice_pdf_processing", lang), cls="ai-showcase__cta-feature"),
            P(t("msg.agentic_record_creation", lang), cls="ai-showcase__cta-feature"),
            Div(
                A(t("msg.get_the_ai_plan", lang),
                  href=subscribe_url("ai"),
                  target="_blank", cls="btn btn--accent"),
                P(t("msg.cancel_anytime", lang), cls="ai-showcase__cta-fine"),
                cls="ai-showcase__cta-actions",
            ),
            cls="ai-showcase__cta-card ai-showcase__cta-card--featured",
        ),
        cls="ai-showcase__cta-cards",
    )


def _free_card(free_questions, owner: bool | None, lang: str) -> FT:
    """The free Celerp account offer on an install with no account. Every role that may
    use the assistant sees the Create button; the sign-up itself checks that the login
    owns the installation, and the note says so only on a confirmed non-owner."""
    return Div(
        P(t("ai.free_no_account_title", lang), cls="ai-showcase__cta-name"),
        P(t("ai.free_no_account_body", lang), cls="ai-showcase__cta-desc"),
        *([P(t("ai.free_questions_included", lang, n=free_questions),
             cls="ai-showcase__cta-feature ai-showcase__cta-feature--highlight"),
           P(t("ai.free_terms", lang), cls="ai-showcase__cta-fine")]
          if isinstance(free_questions, int) and free_questions > 0 else []),
        Div(
            Button(t("ai.free_create_account", lang), type="button",
                   hx_get="/account/panel?intent=signup&panel=account-gate-panel&next=ai&modal=1",
                   hx_target="#account-gate-host", hx_swap="outerHTML",
                   cls="btn btn--primary"),
            *([P(t("ai.free_ask_owner", lang), cls="ai-showcase__cta-fine")] if owner is False else []),
            cls="ai-showcase__cta-actions",
        ),
        cls="ai-showcase__cta-card ai-showcase__free",
    )


def _ai_notice(message: str, lang: str) -> FT:
    """A state of the AI page that the Web Access settings resolve."""
    return Div(
        Div(P(message, cls="ai-notice__text"),
            A(t("doc.send_relay_error_link", lang), href="/settings/cloud",
              cls="btn btn--secondary btn--sm"),
            cls="ai-notice"),
        cls="ai-page",
    )


async def _account_state(token: str, status, lang: str) -> FT | None:
    """The page body for an account situation that has no chat, or None for the chat:
    no account (the showcase with the free offer), signed out, status unknown, or a
    free account without included questions."""
    if not isinstance(status, dict) or status.get("unknown"):
        return Div(load_error(t("ai.service_unavailable", lang), lang, href="/ai"), cls="ai-page")
    if status.get("local"):
        offer, owner = await _ai_offer(token), await api.installation_owner(token)
        return _showcase_view(lang, offer=offer, owner=owner)
    if status.get("disconnected"):
        return _ai_notice(t("error.ai_signed_out", lang), lang)
    if status.get("tier") not in PAID_TIERS and not int(status.get("base_limit") or 0):
        return _ai_notice(t("ai.free_not_eligible", lang), lang)
    return None


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------

def setup_ui_routes(app) -> None:

    @app.get("/ai")
    async def ai_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)

        from celerp.services.permissions import role_has_permission
        try:
            settings = (await api.get_company(token)).get("settings") or {}
        except APIError as exc:
            if exc.status == 401:
                raise
            return await base_shell(
                Div(load_error(t("ai.service_unavailable", lang), lang, href="/ai"), cls="ai-page"),
                title="AI Assistant - Celerp", nav_active="ai", request=request,
            )
        if not role_has_permission(settings, get_role(request), "use_ai_assistant"):
            return not_permitted_redirect(request)

        try:
            status = await api.ai_quota_status(token)
        except Exception:
            status = {"unknown": True}
        state = await _account_state(token, status, lang)
        if state is not None:
            return await base_shell(state, title="AI Assistant - Celerp", nav_active="ai", request=request)
        paid = status.get("tier") in PAID_TIERS
        badge = "" if paid else t(
            "ai.free_questions_left", lang,
            n=int(status.get("remaining") or 0), limit=int(status.get("base_limit") or 0),
        )

        conversation_id = (request.query_params.get("conversation") or "").strip()
        if conversation_id:
            try:
                uuid.UUID(conversation_id)
            except ValueError:
                return RedirectResponse("/ai", status_code=302)
        messages: list[dict] = []
        jobs: list[dict] = []
        failed: FT | None = None
        if conversation_id:
            from celerp.gateway.state import get_session_token
            session_token = get_session_token()
            try:
                conv = await api.ai_conversation_get(token, session_token, conversation_id)
                messages = conv.get("messages") or []
                jobs = conv.get("jobs") or []
            except APIError as exc:
                if exc.status == 401:
                    raise
                if exc.status == 404:
                    return RedirectResponse("/ai", status_code=302)
                failed = load_error(
                    t("ai.conversation_load_failed", lang), lang,
                    href=f"/ai?conversation={conversation_id}",
                )

        return await base_shell(
            _chat_view(
                messages=messages, jobs=jobs, conversation_id=conversation_id,
                lang=lang, failed=failed, paid=paid, badge=badge,
            ),
            title="AI Assistant - Celerp", nav_active="ai", request=request,
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

        # One quota status read feeds the quota section and the usage period.
        try:
            status = await api.ai_quota_status(token, session_token)
        except Exception:
            status = None
        quota_section = _quota_section(status)

        # Per-user usage is company-private administrative data. Hide the section
        # entirely for roles that cannot manage users rather than rendering a 403 as
        # a discouraging "could not load" error. The API independently enforces it.
        from celerp.services.permissions import role_has_permission
        try:
            company_settings = (await api.get_company(token)).get("settings") or {}
        except APIError:
            company_settings = {}
        usage_table = (
            await _usage_table(token, session_token, status)
            if role_has_permission(company_settings, get_role(request), "manage_users")
            else None
        )

        return Div(
            H2(t("page.ai_settings"), cls="ai-settings__title"),
            quota_section,
            *([usage_table] if usage_table is not None else []),
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
        document_mode = "receipts" if form.get("document_mode") == "receipts" else "chat"
        operation_id = (form.get("operation_id") or "").strip()
        # The composer's id names the question it was sent with; a question the user
        # changed since is a new question, so it gets a new id.
        question = _question_key(query, file_ids)
        if form.get("operation_question") not in (None, "", question):
            operation_id = str(uuid.uuid4())

        if not query and not file_ids:
            return _msg_bubble("ai", t("ai.chat_prompt_empty", lang))
        if document_mode == "receipts" and not file_ids:
            return _msg_bubble("ai", t("ai.chat_prompt_empty", lang))

        user_bubble = _msg_bubble("user", query or t("ai.attached_files", lang))

        from celerp.gateway.state import get_session_token
        session_token = get_session_token()

        # The composer keeps the question on every outcome but an answer or a
        # started job (a failed job offers Ask again), and holds the question's id, so sending it
        # again asks the same question instead of a new one. A question id of None
        # (one the API will not take again) gives the composer a new id.
        def _reply(parts: tuple, *, done: bool, question_id: str | None, asked_again: bool = False) -> HTMLResponse:
            if asked_again:
                parts = tuple(p for p in parts if p is not user_bubble)
            headers = {"HX-Push-Url": f"/ai?conversation={conversation_id}"}
            if done:
                headers["X-AI-Clear-Composer"] = "1"
            kept = not done and question_id is not None
            oob = (
                *_operation_input(question_id if kept else str(uuid.uuid4()), question if kept else "", oob=True),
                Input(type="hidden", name="conversation_id", id="ai-conversation-id",
                      value=conversation_id, hx_swap_oob="true"),
                Div(id="ai-history", hx_swap_oob="true",
                    hx_get="/ai/conversations-list", hx_trigger="load", hx_swap="innerHTML"),
            )
            return HTMLResponse(to_xml((*parts, *oob)), headers=headers)

        # An empty conversation id means this is the thread's first message: start
        # the conversation, then run the query inside it. Once created, retain that
        # id even if the query fails so asking again cannot orphan this thread.
        if not conversation_id:
            try:
                title = query[:200] or (
                    t("ai.query_receipts_title", lang)
                    if document_mode == "receipts" else t("ai.attached_files", lang)
                )
                conversation_id = (
                    await api.ai_conversation_create(token, session_token, title=title)
                )["id"]
            except APIError as e:
                if e.status == 401:
                    raise
                return HTMLResponse(to_xml(_failed_reply(user_bubble, e, lang)))

        try:
            result = await api.ai_conversation_query(
                token, session_token, conversation_id, query, file_ids=file_ids,
                document_mode=document_mode, operation_id=operation_id,
            )
        except APIError as e:
            if e.status == 401:
                raise
            data = e.data if isinstance(e.data, dict) else {}
            failure = await _refusal_reply(token, e, user_bubble, query, lang)
            closed = e.status == 409 and _api_error_code(e) == "operation_conflict"
            return _reply(failure, done=False,
                          question_id=None if closed else str(data.get("message_id") or operation_id),
                          asked_again=bool(data.get("asked_again")))

        question_id = str(result.get("message_id") or operation_id)
        asked_again = bool(result.get("asked_again"))
        if result.get("job_id") and document_mode == "receipts":
            # Receipts and invoices are read in the background: the thread shows
            # the job's progress and fetches the bill proposals when it finishes.
            job = {
                "id": result["job_id"], "status": "pending",
                "total_files": len(file_ids or []), "completed_files": 0, "failed_files": 0,
            }
            return _reply((user_bubble, _job_bubble(job, conversation_id, lang)),
                          done=True, question_id=question_id, asked_again=asked_again)
        if result.get("job_id"):
            # A question runs as a job: its bubble polls the question's state and is
            # swapped for the answer, or for the failure with Ask again.
            return _reply((user_bubble, _question_bubble({}, conversation_id, question_id, lang)),
                          done=True, question_id=question_id, asked_again=asked_again)

        error = result.get("error")
        if error:
            again = _failure_action(result, conversation_id, question_id, query, file_ids, lang)
            parts = (user_bubble, _msg_bubble("ai", error, error=True, extra=again))
            # After a failure the service will not take this id again, the composer
            # gets a new id so pressing Send asks a new question.
            return _reply(parts, done=False, question_id=None if result.get("new_question") else question_id,
                          asked_again=asked_again)

        actions = result.get("pending_actions") or []
        message_id = str(actions[0].get("message_id", "")) if actions else ""
        groups = [_action_group(conversation_id, message_id, actions, lang)] if actions else []
        parts = (
            user_bubble,
            _msg_bubble("ai", result.get("answer", ""), help_pages=result.get("help_pages"), lang=lang),
            *groups,
        )
        return _reply(parts, done=True, question_id=question_id, asked_again=asked_again)

    @app.post("/ai/proposals-ui")
    async def ai_proposals_ui(request: Request):
        """The reading job's bubble polls this until the job ends, then it is
        swapped for the finished line, the assistant's summary and the bill cards.
        A POST: the first poll after the job ends saves the proposals message."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        lang = get_lang(request)
        conversation_id = (request.query_params.get("conversation") or "").strip()
        job_id = (request.query_params.get("job") or "").strip()

        from celerp.gateway.state import get_session_token
        session_token = get_session_token()
        try:
            job = await api.ai_batch_status(token, session_token, job_id)
        except APIError as e:
            return _msg_bubble("ai", _api_error_text(e), error=True)
        if job.get("status") != "completed":
            return _job_bubble(job, conversation_id, lang)

        try:
            result = await api.ai_job_proposals(token, session_token, conversation_id, job_id)
        except APIError as e:
            return (
                _job_done_line(job, lang),
                _msg_bubble("ai", _api_error_text(e), error=True),
            )
        message_id = str(result.get("message_id", ""))
        actions = result.get("pending_actions") or []
        groups = [_action_group(conversation_id, message_id, actions, lang)] if actions else []
        return (_job_done_line(job, lang), _msg_bubble("ai", result.get("answer", "")), *groups)

    @app.get("/ai/question-ui")
    async def ai_question_ui(request: Request):
        """A question's pending bubble polls this until its job ends, then it is
        swapped for the answer, or for the failure with what can follow it."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        lang = get_lang(request)
        conversation_id = (request.query_params.get("conversation") or "").strip()
        question_id = (request.query_params.get("question") or "").strip()

        from celerp.gateway.state import get_session_token
        try:
            state = await api.ai_question_state(token, get_session_token(), conversation_id, question_id)
        except APIError as e:
            if e.status == 404:
                return HTMLResponse("")
            # The question's state could not be read this time: say so and read it again.
            return _question_bubble({"notice": _api_error_text(e)}, conversation_id, question_id, lang)
        return await _question_view(token, state, conversation_id, question_id, lang)

    @app.post("/ai/jobs-ui/{job_id}/{action}")
    async def ai_job_action_ui(request: Request, job_id: str, action: str):
        """Stop a running job or continue a stopped one, then show its bubble again:
        a reading job's progress, or a question's pending bubble."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        lang = get_lang(request)
        form = await request.form()
        conversation_id = (form.get("conversation_id") or "").strip()
        is_question = form.get("kind") == "question"
        send = {"cancel": api.ai_job_cancel, "resume": api.ai_job_resume}.get(action)
        if send is None:
            return _R("", status_code=404)

        from celerp.gateway.state import get_session_token
        session_token = get_session_token()
        try:
            await send(token, session_token, job_id)
            if is_question:
                state = await api.ai_question_state(token, session_token, conversation_id, job_id)
                return await _question_view(token, state, conversation_id, job_id, lang)
            return _job_bubble(await api.ai_batch_status(token, session_token, job_id), conversation_id, lang)
        except APIError as e:
            return _msg_bubble("ai", _api_error_text(e), error=True)

    @app.post("/ai/conversations")
    async def ai_conversation_new(request: Request):
        """Legacy HTMX target: New is navigation-only and creates no empty row."""
        from starlette.responses import Response as _R
        if not _token(request):
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        return _R("", headers={"HX-Redirect": "/ai"})

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
            if e.status == 409 and _api_error_code(e) == "action_not_pending":
                return _action_panel(t("ai.action_expired", lang), ok=False)
            return _action_panel(_api_error_text(e), ok=False)
        if result.get("action_status") == "retryable":
            return _retry_action_panel(
                conversation_id, message_id, tool_call_id,
                _outcome_text(result, lang)[1], lang,
            )
        if result.get("action_status") == "pending":
            return _pending_action_panel(
                conversation_id, message_id, tool_call_id,
                _outcome_text(result, lang)[1], lang,
            )
        return _outcome_line(result, lang)

    @app.post("/ai/dismiss-action-ui")
    async def ai_dismiss_action_ui(request: Request):
        """Persist dismissal, then remove the proposal from the rendered thread."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        conversation_id = (form.get("conversation_id") or "").strip()
        message_id = (form.get("message_id") or "").strip()
        tool_call_id = (form.get("tool_call_id") or "").strip()
        from celerp.gateway.state import get_session_token
        session_token = get_session_token()
        try:
            await api.ai_dismiss_action(
                token, session_token, conversation_id, message_id, tool_call_id,
            )
        except APIError as e:
            if e.status == 409 and _api_error_code(e) == "action_not_pending":
                # Another tab may already have handled it. Its desired visible
                # state is still "gone", so make dismissal idempotent in the UI.
                return _R("")
            return _action_panel(_api_error_text(e), ok=False)
        return _R("")

    @app.post("/ai/dismiss-all-ui/{conversation_id}/{message_id}")
    async def ai_dismiss_all_ui(request: Request, conversation_id: str, message_id: str):
        """Dismiss selected open proposals, then reload their canonical thread."""
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        form = await request.form()
        selected = list(dict.fromkeys(
            str(v).strip() for v in form.getlist("selected") if str(v).strip()
        ))
        if not selected:
            return _action_panel(t("ai.action_expired", get_lang(request)), ok=False)
        from celerp.gateway.state import get_session_token
        try:
            await api.ai_dismiss_all(
                token, get_session_token(), conversation_id, message_id, selected,
            )
        except APIError as exc:
            return _action_panel(_api_error_text(exc), ok=False)
        return _R("", headers={"HX-Refresh": "true"})

    @app.post("/ai/confirm-all-ui/{conversation_id}/{message_id}")
    async def ai_confirm_all_ui(request: Request, conversation_id: str, message_id: str):
        """Run one chunk of the selected proposals and report each outcome in place.

        The form carries the selected action ids plus the running tally. Up to
        CONFIRM_CHUNK actions run per request; the reply swaps each finished
        card or table row by id and replaces the tail with the progress line and
        a self-firing form for the rest, or the final tally when nothing remains.
        An API failure mid-batch stops the run and shows the tally so far; the
        untouched rows keep their checkboxes, so the user can run them again.
        """
        token = _token(request)
        lang = get_lang(request)
        if not token:
            return _action_panel(t("msg.not_authenticated", lang), ok=False)
        form = await request.form()
        view = "table" if request.query_params.get("view") == "table" else "cards"
        selected = list(dict.fromkeys(str(v).strip() for v in form.getlist("selected") if str(v).strip()))
        tally = _Tally.from_form(form)
        if not selected:
            return _action_panel(t("ai.action_expired", lang), ok=False)
        chunk, rest = selected[:CONFIRM_CHUNK], selected[CONFIRM_CHUNK:]

        from celerp.gateway.state import get_session_token
        session_token = get_session_token()
        try:
            result = await api.ai_confirm_all(
                token, session_token, conversation_id, message_id, tool_call_ids=chunk,
            )
        except APIError as e:
            if e.status == 409 and _api_error_code(e) == "action_not_pending":
                error = t("ai.action_expired", lang)
            else:
                error = _api_error_text(e)
            return _confirm_tail(conversation_id, message_id, view, [], tally, lang, error=error)
        outcomes = result.get("results") or []
        tally.add(outcomes)
        swaps = [_outcome_swap(message_id, o, view, lang, conversation_id=conversation_id)
                 for o in outcomes]
        tail = _confirm_tail(conversation_id, message_id, view, rest, tally, lang)
        return HTMLResponse(to_xml((tail, *[x for sw in swaps for x in sw])))

    @app.get("/ai/conversations-list")
    async def ai_conversations_list(request: Request):
        token = _token(request)
        if not token:
            return Div()
        try:
            from celerp.gateway.state import get_session_token
            session_token = get_session_token()
            result = await api.ai_conversations_list(token, session_token)
        except Exception as exc:
            if isinstance(exc, APIError) and exc.status == 401:
                raise
            return load_error(
                t("ai.conversations_load_failed", get_lang(request)), get_lang(request),
                hx_get="/ai/conversations-list", target="#ai-history",
            )

        if not result:
            return P(t("msg.no_conversations_yet"), cls="ai-sidebar__empty")

        items = []
        for c in result:
            title = c.get("title") or t("ai.untitled_conversation", get_lang(request))
            open_count = int(c.get("pending_count") or 0)
            badge = [Span(str(open_count), cls="ai-sidebar__badge",
                          title=t("ai.open_proposals", count=open_count))] if open_count else []
            items.append(
                A(
                    Span(title, cls="ai-sidebar__item-title"),
                    *badge,
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
            return load_error(
                t("ai.memory_load_failed", get_lang(request)), get_lang(request),
                hx_get="/ai/memory-panel", target="#ai-memory-content",
            )

        # Shared memory affects every user's assistant context. Mirror the API's
        # manage_company_settings gate in the UI so ordinary AI users get a
        # read-only drawer instead of a button that will inevitably 403.
        from celerp.services.permissions import role_has_permission
        try:
            company_settings = (await api.get_company(token)).get("settings") or {}
        except APIError:
            company_settings = {}
        can_manage_memory = role_has_permission(
            company_settings, get_role(request), "manage_company_settings"
        )

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

        actions = []
        if can_manage_memory:
            actions = [Div(
                Button(t("btn.clear_all_memory"), cls="btn btn--danger btn--sm",
                    hx_delete="/ai/memory-clear",
                    hx_target="#ai-memory-content",
                    hx_confirm=t("ai.clear_memory_confirm"),
                ),
                cls="ai-memory__actions",
            )]
        return Div(
            Div(H4(t("th.notes")), *note_items, cls="ai-memory__section"),
            Div(H4(t("page.facts")), *kv_items, cls="ai-memory__section"),
            *actions,
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
        except APIError as exc:
            return P(_api_error_text(exc), cls="ai-settings__error")
        except Exception:
            return P(t("ai.memory_clear_failed"), cls="ai-settings__error")
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
                result = {**result, "topup_url": topup_url(), "paid": result.get("tier") in PAID_TIERS}
            return JSONResponse(result)
        except Exception:
            return JSONResponse({"unknown": True})


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

def _quota_section(status: dict | None) -> FT:
    """Render quota overview. Falls back to local-install message if no relay.

    ``status`` is None when the quota status failed to load; the ``unknown`` and
    ``disconnected`` sentinels carry no figures, so they render as not loaded too.
    """
    if status is None or status.get("unknown") or status.get("disconnected"):
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
                Span(t("ai.included_credits"), cls="ai-settings__stat-label"),
                Span(str(limit), cls="ai-settings__stat-value"),
                cls="ai-settings__stat",
            ),
            Div(
                Span(t("ai.credits_used"), cls="ai-settings__stat-label"),
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
            Span(
                t("ai.credits_do_not_reset")
                if str(resets_at).lower() == "never"
                else (t("ai.resets_on", date=resets_at) if resets_at else ""),
                cls="ai-settings__reset-date",
            ),
            *([A(t("msg.buy_more_credits"), href=topup_url(), target="_blank",
                 cls="ai-settings__buy-link")] if status.get("tier") in PAID_TIERS else []),
            cls="ai-settings__quota-footer",
        ),
        cls="ai-settings__quota",
    )


async def _usage_table(token: str, session_token: str, status: dict | None) -> FT:
    """Render per-user AI questions: all time on a free account (free questions never
    reset), this month otherwise, including when the quota status is not known."""
    period = "all" if (status or {}).get("tier") == "free" else "month"
    title = t("ai.usage_all_time") if period == "all" else t("page.this_months_usage")
    try:
        data = await api.ai_usage_stats(token, session_token, period=period)
        rows = data.get("users", [])
    except Exception:
        return Div(
            H3(title, cls="ai-settings__section-title"),
            P(t("msg.could_not_load_usage_data"), cls="ai-settings__error"),
        )

    start = (data.get("period") or {}).get("start")
    period_line = t("ai.usage_period_all") if period == "all" else t("ai.usage_period_month", date=start or "--")
    heading = (H3(title, cls="ai-settings__section-title"), P(period_line, cls="ai-settings__text"))

    if not rows:
        empty = t("ai.usage_none_all_time") if period == "all" else t("msg.no_queries_this_month_yet")
        return Div(*heading, P(empty, cls="ai-settings__text"), cls="ai-settings__usage")

    table_rows = [
        Tr(
            Td(r.get("user_name", "--")),
            Td(str(r.get("query_count", 0)), cls="cell--number"),
            Td(str(r.get("credits_used", 0)), cls="cell--number"),
            Td(str(r.get("pending_count", 0) + r.get("unresolved_count", 0)), cls="cell--number"),
            Td((r.get("last_query_at") or "")[:10] or "--"),
        )
        for r in rows
    ]
    unconfirmed = sum(r.get("pending_count", 0) + r.get("unresolved_count", 0) for r in rows)
    unresolved = sum(r.get("unresolved_count", 0) for r in rows)
    notes = [
        *([P(t("ai.usage_unconfirmed_note"), cls="ai-settings__text")] if unconfirmed else []),
        *([P(t("ai.usage_unresolved_note", count=unresolved), cls="ai-settings__text")] if unresolved else []),
    ]

    return Div(
        *heading,
        Table(
            Thead(Tr(
                Th(t("th.user")),
                Th(t("th.queries"), cls="cell--number"),
                Th(t("th.credits_used"), cls="cell--number"),
                Th(t("th.unconfirmed"), cls="cell--number"),
                Th(t("th.last_query")),
            )),
            Tbody(*table_rows),
            cls="data-table ai-settings__usage-table",
        ),
        *notes,
        P(t("ai.usage_scope_note"), cls="ai-settings__text"),
        cls="ai-settings__usage",
    )


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def _msg_bubble(role: str, text: str, *, error: bool = False, help_pages: list[str] | None = None,
                lang: str = "en", extra: FT | None = None) -> FT:
    cls = "ai-msg ai-msg--user" if role == "user" else "ai-msg ai-msg--ai"
    if error:
        cls += " ai-msg--error"
    return Div(text, *_help_sources(help_pages, lang), *([extra] if extra is not None else []), cls=cls)


def _question_key(query: str, file_ids: list[str] | None) -> str:
    """The text and files of a question, as the composer records the question its id names."""
    return json.dumps([query, file_ids or []], ensure_ascii=False)


def _operation_input(question_id: str, question: str = "", *, oob: bool = False) -> tuple[FT, FT]:
    """The composer's question id and the question it was sent with ("" before it is
    sent): sending the same question again with it asks the AI service the same
    question rather than a new one."""
    swap = {"hx_swap_oob": "true"} if oob else {}
    return (
        Input(type="hidden", name="operation_id", id="ai-operation-id", value=question_id, **swap),
        Input(type="hidden", name="operation_question", id="ai-operation-question", value=question, **swap),
    )


def _ask_again(conversation_id: str, question_id: str, query: str, file_ids: list[str] | None,
               lang: str) -> FT:
    """Ask a question that failed once more, as the same question."""
    return _resend(conversation_id, question_id, query, file_ids, t("ai.ask_again", lang), "ai-msg__again")


def _ask_as_new(conversation_id: str, query: str, file_ids: list[str] | None, lang: str) -> FT:
    """Ask a question the AI service will not take again as a new question, under a new
    id; the reply above it says this counts as one more question."""
    return _resend(conversation_id, str(uuid.uuid4()), query, file_ids, t("ai.ask_as_new", lang), "ai-msg__new")


def _failure_action(result: dict, conversation_id: str, question_id: str, query: str,
                    file_ids: list[str] | None, lang: str) -> FT | None:
    """The button under a failed reply: ask again, ask as a new question, or none."""
    if result.get("retryable"):
        return _ask_again(conversation_id, question_id, query, file_ids, lang)
    if result.get("new_question"):
        return _ask_as_new(conversation_id, query, file_ids, lang)
    return None


def _resend(conversation_id: str, question_id: str, query: str, file_ids: list[str] | None,
            label: str, cls: str) -> FT:
    return Button(
        label, type="button", cls=f"btn btn--secondary btn--sm {cls}",
        hx_post="/ai/chat", hx_target="#ai-messages", hx_swap="beforeend",
        hx_vals=json.dumps({
            "query": query, "file_ids": ",".join(file_ids or []),
            "conversation_id": conversation_id, "operation_id": question_id,
        }),
        hx_on__after_request="celerpAiAfterRequest(event); this.remove();",
    )


def _help_sources(page_ids: list[str] | None, lang: str) -> list[FT]:
    """The help pages an answer came from, or the note that none covers it. Nothing when
    the answer did not search help, or when every page it read has since gone."""
    if page_ids is None:
        return []
    if not page_ids:
        return [P(t("ai.help_no_coverage", lang), cls="ai-msg__sources")]
    from celerp.help.registry import get_index
    pages = get_index().pages
    links = [(i, pages[i].title) for i in page_ids if i in pages]
    if not links:
        return []
    return [P(t("ai.help_sources", lang), " ", *_page_links(links), cls="ai-msg__sources")]


def _page_links(pages: list[tuple[str, str]]) -> list:
    """Comma-separated links to help pages, from (id, title) pairs."""
    links = [A(title, href=f"/help/{page_id}") for page_id, title in pages]
    return [part for n, link in enumerate(links) for part in ((", ", link) if n else (link,))]


def _failed_reply(user_bubble: FT, e: APIError, lang: str) -> tuple[FT, FT]:
    """The user's bubble followed by the assistant's note that the request failed.

    Returned as a fragment, never wrapped: each bubble must be a direct child of
    the message column so it takes the same alignment as a reloaded thread.
    """
    if e.status == 429:
        return user_bubble, _msg_bubble("ai", t("ai.busy", lang))
    return user_bubble, _msg_bubble("ai", _api_error_text(e), error=True)


def _api_error_code(e: APIError) -> str:
    """The API's typed error code, from the 409 detail dict."""
    for source in (e.data, e.detail):
        if isinstance(source, dict) and source.get("code"):
            return str(source["code"])
    return ""


def _api_error_text(e: APIError) -> str:
    detail = e.detail.get("message") or e.detail.get("code") if isinstance(e.detail, dict) else e.detail
    return str(detail)


def _outcome_text(outcome: dict, lang: str = "en") -> tuple[bool, str]:
    """(ok, text) for one confirmed action's outcome, success or failure."""
    if outcome.get("ok"):
        return True, _confirm_success_text(outcome.get("data"), lang)
    error = outcome.get("error") or {}
    message = (error.get("message") or error.get("code")) if isinstance(error, dict) else str(error)
    return False, str(message)


def _label(key: str) -> str:
    """A field name as a person reads it: contact_name becomes Contact name."""
    return str(key).replace("_", " ").strip().capitalize()


def _fmt_scalar(key: str, value, currency: str | None) -> str:
    """One field value as a person reads it: quantities without float noise,
    amounts at currency precision, everything else as text."""
    if value is None or value == "":
        return "--"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if key in QTY_FIELD_KEYS:
            return fmt_qty(value)
        if is_money_field(key):
            return fmt_price(value, key, currency)
    return str(value)


def _fmt_value(key: str, value, currency: str | None = None) -> FT | str:
    """A field value as a person reads it; nested records become short lines."""
    if isinstance(value, dict):
        parts = [f"{_label(k)}: {_fmt_scalar(k, v, currency)}"
                 for k, v in value.items() if v not in (None, "")]
        return ", ".join(parts) or "--"
    if isinstance(value, list):
        if not value:
            return "--"
        if all(not isinstance(v, (dict, list)) for v in value):
            return ", ".join(_fmt_scalar(key, v, currency) for v in value)
        return Ol(*[Li(_fmt_value(key, v, currency)) for v in value], cls="ai-action__sublist")
    return _fmt_scalar(key, value, currency)


def _arg_lines(section: dict) -> FT:
    """A definition-style list of one argument group (body, path, or query).

    A record id is hidden when the same section names the record (contact_id
    next to contact_name): the name is what the person checks.
    """
    currency = section.get("currency") if isinstance(section.get("currency"), str) else None
    lines = []
    for key, value in section.items():
        if str(key).endswith("_id") and f"{str(key)[:-3]}_name" in section:
            continue
        lines.append(Div(
            Span(_label(key), cls="ai-action__line-key"),
            Span(_fmt_value(str(key), value, currency), cls="ai-action__line-val"),
            cls="ai-action__line",
        ))
    return Div(*lines, cls="ai-action__lines")


def _action_title(action: dict) -> str:
    return action.get("title") or _label(action.get("name", ""))


def _action_dom_id(message_id: str, tool_call_id: str) -> str:
    """The element id a proposal's card or table row carries, so an outcome can
    replace it out of band after a bulk confirm."""
    return f"ai-act-{message_id}-{tool_call_id}"


def _action_body(action: dict, lang: str = "en") -> list[FT]:
    """What a proposal will write and what to check first: the argument groups
    and the warnings block, shared by the card and the table's detail row."""
    args = action.get("arguments") or {}
    groups = [
        _arg_lines(args[part])
        for part in ("body", "path", "query")
        if isinstance(args.get(part), dict) and args[part]
    ]
    out: list[FT] = [Div(*groups, cls="ai-action__list")]
    warnings = _action_warnings(action)
    if warnings:
        out.append(Div(
            P(t("ai.action_check", lang), cls="ai-action__warnings-title"),
            Ul(*[Li(w) for w in warnings], cls="ai-action__warnings-list"),
            cls="ai-action__warnings",
        ))
    return out


def _action_warnings(action: dict) -> list[str]:
    return [w for w in (action.get("warnings") or []) if w]


def _action_card(conversation_id: str, message_id: str, action: dict, lang: str = "en") -> FT:
    """Render one proposed change the user must confirm before it runs.

    The card names the change, lists what will be written, and flags anything
    the reader should check first. Confirm and Dismiss post identifiers only
    (never mutation arguments), so the server owns both execution and proposal
    state. Retryable actions reuse the same immutable tool-call identity.
    """
    name = action.get("name", "")
    tool_call_id = action.get("id", "")
    status = action.get("status", "pending")
    failed = status == "failed"
    retryable = status == "retryable"
    if failed:
        badge = Span(t("ai.action_failed_badge", lang), cls="badge badge--error")
        footer = Div(action.get("error") or t("ai.action_failed", lang), cls="ai-action__error")
    else:
        badge = Span(t("btn.retry", lang) if retryable else t("ai.action_proposal", lang),
                     cls="badge badge--proposal")
        footer = Div(
            P(action.get("error") or t("ai.action_hint", lang),
              cls="ai-action__error" if retryable else "ai-action__hint"),
            Div(
                Form(
                    Input(type="hidden", name="conversation_id", value=conversation_id),
                    Input(type="hidden", name="message_id", value=message_id),
                    Input(type="hidden", name="tool_call_id", value=tool_call_id),
                    Button(t("btn.retry", lang) if retryable else t("btn.confirm", lang),
                           type="submit", cls="btn btn--primary"),
                    hx_post="/ai/confirm-action-ui",
                    hx_target="closest .ai-action__card",
                    hx_swap="outerHTML",
                ),
                Form(
                    Input(type="hidden", name="conversation_id", value=conversation_id),
                    Input(type="hidden", name="message_id", value=message_id),
                    Input(type="hidden", name="tool_call_id", value=tool_call_id),
                    Button(t("btn.dismiss", lang), type="submit", cls="btn btn--secondary"),
                    hx_post="/ai/dismiss-action-ui",
                    hx_target="closest .ai-action__card",
                    hx_swap="outerHTML",
                ),
                cls="ai-action__actions",
            ),
            cls="ai-action__footer",
        )
    return Div(
        Div(P(_action_title(action), cls="ai-action__title"), badge, cls="ai-action__header"),
        *_action_body(action, lang),
        footer,
        id=_action_dom_id(message_id, tool_call_id),
        cls="ai-action ai-action__card" + (" ai-action--failed" if failed else ""),
        data_capability=name,
    )


def _open_action_panel(
    conversation_id: str, message_id: str, tool_call_id: str,
    error: str, lang: str = "en", *, retryable: bool, oob: bool = False,
) -> FT:
    """Compact actionable replacement for an open pending/retryable proposal."""
    attrs = {"hx_swap_oob": "true"} if oob else {}
    return Div(
        Div(error, cls="ai-action__error"),
        Div(
            Form(
                Input(type="hidden", name="conversation_id", value=conversation_id),
                Input(type="hidden", name="message_id", value=message_id),
                Input(type="hidden", name="tool_call_id", value=tool_call_id),
                Button(t("btn.retry", lang) if retryable else t("btn.confirm", lang),
                       type="submit", cls="btn btn--primary"),
                hx_post="/ai/confirm-action-ui", hx_target="closest .ai-action__card",
                hx_swap="outerHTML",
            ),
            Form(
                Input(type="hidden", name="conversation_id", value=conversation_id),
                Input(type="hidden", name="message_id", value=message_id),
                Input(type="hidden", name="tool_call_id", value=tool_call_id),
                Button(t("btn.dismiss", lang), type="submit", cls="btn btn--secondary"),
                hx_post="/ai/dismiss-action-ui", hx_target="closest .ai-action__card",
                hx_swap="outerHTML",
            ),
            cls="ai-action__actions",
        ),
        id=_action_dom_id(message_id, tool_call_id),
        cls="ai-action ai-action__card", **attrs,
    )


def _retry_action_panel(conversation_id: str, message_id: str, tool_call_id: str,
                        error: str, lang: str = "en", *, oob: bool = False) -> FT:
    return _open_action_panel(
        conversation_id, message_id, tool_call_id, error, lang,
        retryable=True, oob=oob,
    )


def _pending_action_panel(conversation_id: str, message_id: str, tool_call_id: str,
                          error: str, lang: str = "en", *, oob: bool = False) -> FT:
    return _open_action_panel(
        conversation_id, message_id, tool_call_id, error, lang,
        retryable=False, oob=oob,
    )


# Field names a proposal's body is summarised by in the review table, first match wins.
_SUMMARY_NAME_KEYS = ("contact_name", "name", "sku", "title")
_SUMMARY_DATE_KEYS = ("issue_date", "date")
_SUMMARY_AMOUNT_KEYS = ("total", "amount", "quantity")


def _summary_field(body: dict, keys: tuple[str, ...]) -> str:
    currency = body.get("currency") if isinstance(body.get("currency"), str) else None
    for key in keys:
        value = body.get(key)
        if value not in (None, "") and not isinstance(value, (dict, list)):
            return _fmt_scalar(key, value, currency)
    return "--"


def _action_status_cell(action: dict, lang: str = "en") -> list:
    status = action.get("status", "pending")
    if status == "failed":
        return [Span(t("ai.action_failed_badge", lang), cls="badge badge--error"),
                Span(action.get("error") or t("ai.action_failed", lang), cls="ai-action__error")]
    if status == "retryable":
        return [Span(t("btn.retry", lang), cls="badge badge--proposal"),
                Span(action.get("error") or "", cls="ai-action__error")]
    return [Span(t("ai.action_proposal", lang), cls="badge badge--proposal")]


def _action_row(message_id: str, action: dict, lang: str = "en") -> tuple[FT, FT]:
    """One proposal as a review-table row plus its hidden detail row.

    A proposal with warnings starts unticked, its reasons in the Checks column,
    so a bulk confirm applies only what needs no second look unless the user
    ticks it. A failed record has no checkbox.
    """
    tool_call_id = action.get("id", "")
    dom_id = _action_dom_id(message_id, tool_call_id)
    body = (action.get("arguments") or {}).get("body")
    body = body if isinstance(body, dict) else {}
    warnings = _action_warnings(action)
    status = action.get("status", "pending")
    open_action = status in {"pending", "retryable"}
    pick = []
    if open_action:
        box = {"type": "checkbox", "cls": "bulk-select", "name": "selected", "value": tool_call_id}
        if status == "pending" and not warnings:
            box["checked"] = True
        pick = [Input(**box)]
    row = Tr(
        Td(*pick, id=f"{dom_id}-pick"),
        Td(Button(_action_title(action), type="button", cls="ai-action-table__toggle",
                  title=t("th.details", lang),
                  onclick="this.closest('tr').nextElementSibling.classList.toggle('is-open')")),
        Td(_summary_field(body, _SUMMARY_NAME_KEYS)),
        Td(_summary_field(body, _SUMMARY_DATE_KEYS)),
        Td(_summary_field(body, _SUMMARY_AMOUNT_KEYS), cls="cell--number"),
        Td(Div("; ".join(warnings) if warnings else "--", cls="ai-action-table__checks-text"), cls="ai-action-table__checks"),
        Td(*_action_status_cell(action, lang), id=f"{dom_id}-status", cls="ai-action-table__status"),
        id=dom_id, cls="data-row", data_capability=action.get("name", ""),
    )
    details = Tr(Td(*_action_body(action, lang), colspan="7"), cls="ai-action-table__details")
    return row, details


def _action_table(conversation_id: str, message_id: str, actions: list[dict], lang: str = "en") -> FT:
    """The review table for a large proposal set: tick rows, then Confirm selected
    from the shared bulk toolbar; each row expands to the card's detail."""
    from ui.components.table import bulk_toolbar
    table_id = f"ai-actions-{message_id}"
    rows = [part for a in actions for part in _action_row(message_id, a, lang)]
    table = Table(
        Thead(Tr(
            Th(Input(type="checkbox", cls="bulk-select-all")),
            Th(t("ai.col_change", lang)),
            Th(t("label.name", lang)),
            Th(t("th.date", lang)),
            Th(t("label.amount", lang), cls="cell--number"),
            Th(t("ai.col_checks", lang)),
            Th(t("label.status", lang)),
        )),
        Tbody(*rows),
        id=table_id, cls="data-table ai-action-table",
    )
    toolbar = bulk_toolbar(table_id, [
        {
            "value": "confirm", "label": t("ai.confirm_selected", lang), "method": "post",
            "url": f"/ai/confirm-all-ui/{conversation_id}/{message_id}?view=table",
            "target": f"#ai-tail-{message_id}", "swap": "outerHTML",
        },
        {
            "value": "dismiss", "label": t("ai.dismiss_selected", lang), "method": "post",
            "url": f"/ai/dismiss-all-ui/{conversation_id}/{message_id}",
            "target": f"#ai-tail-{message_id}", "swap": "outerHTML",
            "confirm": t("ai.dismiss_selected_confirm", lang, n="{n}"),
        },
    ])
    return Div(
        Div(table, cls="table-scroll-wrap"),
        toolbar,
        Div(id=f"ai-tail-{message_id}", cls="ai-action-group__footer"),
        cls="ai-action-group ai-action-group--table",
    )


def _action_group(conversation_id: str, message_id: str, actions: list[dict], lang: str = "en") -> FT:
    """The proposals one assistant turn made: cards with one Confirm all button
    when more than one is open, or the review table once the set is large."""
    if len(actions) > TABLE_FROM:
        return _action_table(conversation_id, message_id, actions, lang)
    cards = [_action_card(conversation_id, message_id, a, lang) for a in actions]
    open_ids = [a.get("id", "") for a in actions if a.get("status", "pending") == "pending"]
    footer = []
    if len(open_ids) > 1:
        footer = [Form(
            *[Input(type="hidden", name="selected", value=i) for i in open_ids],
            Button(t("ai.confirm_all", lang, count=len(open_ids)), type="submit", cls="btn btn--primary"),
            hx_post=f"/ai/confirm-all-ui/{conversation_id}/{message_id}?view=cards",
            hx_target=f"#ai-tail-{message_id}",
            hx_swap="outerHTML",
            id=f"ai-tail-{message_id}",
            cls="ai-action-group__footer",
        )]
    return Div(*cards, *footer, cls="ai-action-group")


# UI paths of the records the confirmed capabilities create, by operation id fragment.
_RECORD_PATHS = (("_docs_", "/docs/{id}"), ("_crm_contacts", "/contacts/{id}"), ("_items_", "/inventory/{id}"))


def _record_link(outcome: dict) -> str | None:
    """Where the record a successful action created lives in the UI, if known."""
    data = outcome.get("data")
    ident = (data.get("id") or data.get("entity_id")) if isinstance(data, dict) else None
    if not outcome.get("ok") or not ident:
        return None
    name = str(outcome.get("name") or "")
    for needle, path in _RECORD_PATHS:
        if needle in name:
            return path.format(id=ident)
    return None


def _outcome_line(outcome: dict, lang: str = "en", *, message_id: str | None = None) -> FT:
    """The line that replaces a confirmed card: the action by name, what happened,
    and a link to the record when one was created."""
    ok, text = _outcome_text(outcome, lang)
    title = outcome.get("title") or ""
    link = _record_link(outcome)
    parts = [f"{title}: {text}" if title else text]
    if link:
        parts += [" ", A(t("ai.open_record", lang), href=link, cls="table-link")]
    attrs = {}
    if message_id is not None:
        attrs = {"id": _action_dom_id(message_id, outcome.get("tool_call_id", "")), "hx_swap_oob": "true"}
    return Div(*parts, cls="ai-action__done" if ok else "ai-action__error", **attrs)


def _outcome_swap(message_id: str, outcome: dict, view: str, lang: str = "en",
                  *, conversation_id: str = "") -> list[FT]:
    """The out-of-band elements that mark one action finished in the thread."""
    action_status = outcome.get("action_status")
    retryable = action_status == "retryable"
    pending = action_status == "pending"
    if view != "table":
        if retryable:
            return [_retry_action_panel(
                conversation_id, message_id, outcome.get("tool_call_id", ""),
                _outcome_text(outcome, lang)[1], lang, oob=True,
            )]
        if pending:
            return [_pending_action_panel(
                conversation_id, message_id, outcome.get("tool_call_id", ""),
                _outcome_text(outcome, lang)[1], lang, oob=True,
            )]
        return [_outcome_line(outcome, lang, message_id=message_id)]
    dom_id = _action_dom_id(message_id, outcome.get("tool_call_id", ""))
    ok, text = _outcome_text(outcome, lang)
    link = _record_link(outcome)
    if retryable or pending:
        cell = [Span(t("btn.retry", lang) if retryable else t("ai.action_proposal", lang),
                     cls="badge badge--proposal"),
                Span(text, cls="ai-action__error")]
        pick = Td(Input(type="checkbox", cls="bulk-select", name="selected",
                        value=outcome.get("tool_call_id", "")),
                  id=f"{dom_id}-pick", hx_swap_oob="true")
    elif ok:
        cell = [Span(t("th.applied", lang), cls="badge badge--active")]
        if link:
            cell += [" ", A(t("ai.open_record", lang), href=link, cls="table-link")]
        pick = Td(id=f"{dom_id}-pick", hx_swap_oob="true")
    else:
        cell = [Span(t("ai.action_failed_badge", lang), cls="badge badge--error"),
                Span(text, cls="ai-action__error")]
        pick = Td(id=f"{dom_id}-pick", hx_swap_oob="true")
    # Table cells cannot stand on their own in a fragment; htmx reads them out
    # of a template wrapper and swaps each by id.
    return [Template(
        pick,
        Td(*cell, id=f"{dom_id}-status", cls="ai-action-table__status", hx_swap_oob="true"),
    )]


class _Tally:
    """The running result of a chunked confirm, carried between requests in the
    tail form: counts so far and the ids of the drafts created."""

    def __init__(
        self, completed: int = 0, failed: int = 0, attention: int = 0,
        doc_ids: list[str] | None = None,
    ):
        self.completed = completed
        self.failed = failed
        self.attention = attention
        self.doc_ids = doc_ids or []

    @classmethod
    def from_form(cls, form) -> "_Tally":
        def _int(key: str) -> int:
            try:
                return max(0, int(form.get(key) or 0))
            except (TypeError, ValueError):
                return 0
        doc_ids = [x.strip() for x in str(form.get("doc_ids") or "").split(",") if x.strip()]
        return cls(_int("completed"), _int("failed"), _int("attention"), doc_ids)

    def add(self, outcomes: list[dict]) -> None:
        for o in outcomes:
            if o.get("ok"):
                self.completed += 1
                data = o.get("data")
                if "_docs_" in str(o.get("name") or "") and isinstance(data, dict) and data.get("id"):
                    self.doc_ids.append(str(data["id"]))
            elif o.get("action_status") in {"pending", "retryable"}:
                self.attention += 1
            else:
                self.failed += 1

    def inputs(self) -> list[FT]:
        return [
            Input(type="hidden", name="completed", value=str(self.completed)),
            Input(type="hidden", name="failed", value=str(self.failed)),
            Input(type="hidden", name="attention", value=str(self.attention)),
            Input(type="hidden", name="doc_ids", value=",".join(self.doc_ids)),
        ]


def _confirm_tail(conversation_id: str, message_id: str, view: str, rest: list[str],
                  tally: _Tally, lang: str = "en", *, error: str | None = None) -> FT:
    """The footer under a proposal set while a bulk confirm runs and after it ends.

    With ids left and no error it shows how far the run is and carries a form
    that fires on load for the next chunk. Otherwise it is the tally, any error
    that stopped the run, and a link to the drafts the batch created.
    """
    done = tally.completed + tally.failed + tally.attention
    if rest and not error:
        return Div(
            P(t("ai.confirm_progress", lang, done=done, total=done + len(rest)), cls="ai-action-group__summary"),
            Form(
                *[Input(type="hidden", name="selected", value=i) for i in rest],
                *tally.inputs(),
                hx_post=f"/ai/confirm-all-ui/{conversation_id}/{message_id}?view={view}",
                hx_trigger="load",
                hx_target=f"#ai-tail-{message_id}",
                hx_swap="outerHTML",
            ),
            id=f"ai-tail-{message_id}", cls="ai-action-group__footer",
        )
    summary = (
        t("ai.confirm_all_result_attention", lang, completed=tally.completed,
          failed=tally.failed, attention=tally.attention)
        if tally.attention
        else t("ai.confirm_all_result", lang, completed=tally.completed, failed=tally.failed)
        if tally.failed
        else t("ai.confirm_all_done", lang, completed=tally.completed)
    )
    children: list = [P(summary, cls="ai-action-group__summary")]
    if error:
        children.append(Div(error, cls="ai-action__error"))
    if tally.doc_ids:
        children.append(A(
            t("ai.open_drafts", lang, n=len(tally.doc_ids)),
            href=f"/docs?view=drafts&ids={','.join(tally.doc_ids)}",
            cls="btn btn--secondary",
        ))
    return Div(*children, id=f"ai-tail-{message_id}", cls="ai-action-group__footer")


def _job_counts(job: dict) -> tuple[int, int, int]:
    """(read, failed, total) file counts of a reading job."""
    return (
        int(job.get("completed_files") or 0),
        int(job.get("failed_files") or 0),
        int(job.get("total_files") or 0),
    )


def _job_done_line(job: dict, lang: str = "en") -> FT:
    ok, _failed, total = _job_counts(job)
    return Div(t("ai.job_done", lang, ok=ok, total=total),
               cls="ai-msg ai-msg--ai ai-job ai-job--done", data_job_id=str(job.get("id", "")))


def _pending_bubble(poll_url: str, delay_s: float, *children: FT, cls: str, also_on: str = "",
                    method: str = "get", **attrs) -> FT:
    """A bubble that calls ``poll_url`` after ``delay_s`` seconds (at once for 0) and is
    replaced by what it returns: the one way the thread follows a running job. A poll
    that may save something (``method="post"``) is never a GET."""
    poll = f"load delay:{delay_s:g}s" if delay_s else "load"
    return Div(*children, cls=cls, hx_trigger=", ".join(filter(None, (poll, also_on))),
               hx_swap="outerHTML", **{f"hx_{method}": poll_url}, **attrs)


def _job_button(job_id: str, action: str, label: str, conversation_id: str, *, target: str,
                kind: str = "documents") -> FT:
    """Stop a running job or continue a stopped one; the job's bubble is shown again."""
    return Button(label, type="button", cls="btn btn--secondary btn--sm",
                  hx_post=f"/ai/jobs-ui/{job_id}/{action}", hx_target=f"#{target}", hx_swap="outerHTML",
                  hx_vals=json.dumps({"conversation_id": conversation_id, "kind": kind}))


def _job_bubble(job: dict, conversation_id: str, lang: str = "en") -> FT:
    """The assistant-side bubble for a receipts reading job.

    While the job runs the bubble shows how many files are read, updates from
    the notification stream, polls the proposals route so the cards appear
    without a reload, and offers Stop. A stopped job, or one nobody runs any
    more, offers Continue. A failed job
    is an error bubble; a finished job whose proposals already exist is the
    finished line.
    """
    job_id = str(job.get("id", ""))
    status = job.get("status")
    ok, failed, total = _job_counts(job)
    dom_id = f"ai-job-{job_id}"
    if status == "failed":
        return Div(
            Div(job.get("error") or t("ai.job_failed", lang)),
            Button(t("ai.attach_files_again", lang), type="button",
                   cls="btn btn--secondary btn--sm", onclick="celerpAiOpenFilePicker()"),
            cls="ai-msg ai-msg--ai ai-msg--error", data_job_id=job_id,
        )
    if status == "completed" and job.get("proposal_message_id"):
        return _job_done_line(job, lang)
    done = ok + failed
    if status in ("cancelled", "interrupted"):
        # Stopped by its user, or left by a runner that is gone: either continues.
        said = "ai.job_stopped" if status == "cancelled" else "ai.job_interrupted"
        return Div(
            Div(t(said, lang, done=done, total=total), cls="ai-job__text"),
            _job_button(job_id, "resume", t("btn.continue", lang), conversation_id, target=dom_id),
            id=dom_id, cls="ai-msg ai-msg--ai ai-job", data_job_id=job_id,
        )
    if status == "completed":
        delay = 0
    else:
        delay = 1 if total and done >= total else 5
    pct = round(done / total * 100) if total else 0
    stop = [] if status == "completed" else [
        _job_button(job_id, "cancel", t("ai.stop", lang), conversation_id, target=dom_id)]
    return _pending_bubble(
        f"/ai/proposals-ui?conversation={conversation_id}&job={job_id}", delay,
        Div(t("ai.job_reading", lang, done=done, total=total), cls="ai-job__text"),
        Div(Div(cls="ai-job__bar", style=f"width:{pct}%"), cls="ai-job__track"),
        P(t("ai.job_reading_hint", lang), cls="ai-job__hint"),
        *stop,
        cls="ai-msg ai-msg--ai ai-job", also_on="celerp:job-done", method="post",
        id=dom_id,
        data_job_id=job_id,
        data_total=str(total),
        data_text=t("ai.job_reading", lang, done="{done}", total="{total}"),
    )


QUESTION_POLL_S = 2


def _question_bubble(state: dict, conversation_id: str, question_id: str, lang: str = "en") -> FT:
    """A running question: the typing dots, what it waits for or that it moved to the
    background, and Stop once it did; it polls the question's state."""
    dom_id = f"ai-q-{question_id}"
    notice = state.get("notice")
    stop = [_job_button(question_id, "cancel", t("ai.stop", lang), conversation_id, target=dom_id,
                        kind="question")] if state.get("promoted") else []
    return _pending_bubble(
        f"/ai/question-ui?conversation={conversation_id}&question={question_id}", QUESTION_POLL_S,
        Div(*(Span(cls="ai-typing__dot") for _ in range(3)), cls="ai-typing"),
        *([P(notice, cls="ai-msg__notice")] if notice else []),
        *stop,
        cls="ai-msg ai-msg--ai ai-msg--pending", id=dom_id,
    )


async def _question_view(token: str, state: dict, conversation_id: str, question_id: str,
                         lang: str = "en") -> FT | tuple:
    """A question as its state says: running, interrupted by a restart (Ask again
    continues it), refused by the AI service (as a refused send is shown), or ended
    with its stored reply, shown as the thread shows it."""
    asked = state.get("asked") or {"id": question_id}
    if state.get("state") == "running":
        return _question_bubble(state, conversation_id, question_id, lang)
    if state.get("state") == "interrupted":
        again = _ask_again(conversation_id, question_id, asked.get("content") or "", asked.get("file_ids"), lang)
        return _msg_bubble("ai", t("ai.question_interrupted", lang), error=True, extra=again)
    message = state["message"]
    if message.get("refusal") is not None:
        return await _refused(token, message.get("refusal_status"), message["refusal"],
                              asked.get("content") or "", lang)
    return tuple(_message_parts(message, conversation_id, asked, lang, offer_again=True))


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
        Button(
            Span(q["icon"], cls="ai-empty-state__card-icon"),
            Div(
                Strong(q["title"], cls="ai-empty-state__card-title"),
                P(q["query"], cls="ai-empty-state__card-query"),
                cls="ai-empty-state__card-body",
            ),
            type="button", cls="ai-empty-state__card",
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


def _showcase_view(lang: str = "en", *, offer: dict | None = None, owner: bool | None = None) -> FT:
    """The page for an install with no Celerp account: what the assistant does, the
    free account offer and the paid plans. ``offer`` is the AI offer (counts), ``owner``
    whether this login owns the installation (None when that could not be checked)."""
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
                _free_card((offer or {}).get("free_ai_questions"), owner, lang),
                P(t("msg.stop_doing_manual_data_entry_hire_an_ai_operator"),
                  cls="ai-showcase__cta-headline"),
                _plan_cards((offer or {}).get("plan_ai_questions") or {}, lang),
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


def _thread(messages: list[dict], jobs: list[dict], conversation_id: str, lang: str) -> list[FT]:
    """Render a stored conversation in time order: message bubbles, reading-job
    bubbles, and the action cards beneath the assistant turn that proposed them. A
    last question with no reply yet and no reading job is a running or interrupted
    question job: its bubble reads its state."""
    entries = [("message", m) for m in messages] + [("job", j) for j in jobs]
    entries.sort(key=lambda entry: str(entry[1].get("created_at") or ""))
    out: list[FT] = []
    # Only the thread's last reply, when it is a failure, offers to ask the question
    # before it again or as a new question.
    last = messages[-1] if messages else None
    asked: dict | None = None
    for kind, entry in entries:
        if kind == "job":
            out.append(_job_bubble(entry, conversation_id, lang))
            continue
        out.extend(_message_parts(entry, conversation_id, asked, lang, offer_again=entry is last))
        if entry.get("role") == "user":
            asked = entry
    reading = {str(j.get("id")) for j in jobs}
    if last is not None and last.get("role") == "user" and str(last.get("id")) not in reading:
        out.append(_question_bubble({}, conversation_id, str(last.get("id")), lang))
    return out


def _message_parts(entry: dict, conversation_id: str, asked: dict | None, lang: str, *,
                   offer_again: bool) -> list[FT]:
    """A stored message as the thread shows it: its bubble (a refusal explained, a
    failure with Ask again or Ask as new when ``offer_again`` and its question is
    known), then the action cards it proposed."""
    role = entry.get("role")
    text = entry.get("content") or (t("ai.attached_files", lang) if entry.get("file_ids") else "")
    error = bool(entry.get("error"))
    if entry.get("refusal") is not None:
        explained = _refusal_text(entry.get("refusal_status"), entry["refusal"], lang)
        if explained is not None:
            text, error = explained
    again = None
    if offer_again and error and asked is not None:
        again = _failure_action(entry, conversation_id, str(asked.get("id", "")),
                                asked.get("content") or "", asked.get("file_ids"), lang)
    out = [_msg_bubble("user" if role == "user" else "ai", text, error=error,
                       help_pages=entry.get("help_pages"), lang=lang, extra=again)]
    actions = entry.get("pending_actions") or []
    if actions:
        out.append(_action_group(conversation_id, str(entry.get("id", "")), actions, lang))
    return out


def _chat_view(
    messages: list[dict] | None = None, jobs: list[dict] | None = None,
    conversation_id: str = "", lang: str = "en", failed: FT | None = None,
    paid: bool = True, badge: str = "",
) -> FT:
    """The chat. ``paid`` offers the top-up link; a free account's ``badge`` (its
    questions left) is rendered here, a paid plan's credits are read by the page script."""
    if failed is not None:
        message_children = [failed]
    elif messages or jobs:
        message_children = _thread(messages or [], jobs or [], conversation_id, lang)
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
                A(t("btn._new", lang), href="/ai",
                  cls="btn btn--secondary ai-sidebar__new"),
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
                A("+", href="/ai", cls="ai-sidebar__icon-btn",
                  title=t("ai.new_conversation", lang)),
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
                Span(badge, id="ai-quota-display", cls="ai-quota"),
                *([A(t("msg.buy_more_credits", lang), id="ai-topup-link", href="#",
                     target="_blank", cls="ai-topup-link", style="display:none;")] if paid else []),
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
                    accept="image/jpeg,image/png,image/webp,application/pdf,.csv,.xlsx",
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
                *_operation_input(str(uuid.uuid4())),
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
                    Button(
                        t("ai.query_receipts_title", lang),
                        type="submit", name="document_mode", value="receipts",
                        cls="btn btn--secondary ai-input__receipts",
                        title=t("ai.query_receipts_title", lang), disabled=True,
                    ),
                    cls="ai-input__row",
                ),
                # Drop zone
                Div(
                    Span("📁", cls="ai-chat-dropzone__icon"),
                    Span(t("msg.drop_files_here_or_click_to_browse", lang), cls="ai-chat-dropzone__label"),
                    Div(id="ai-file-chips", cls="ai-file-chips"),
                    id="ai-chat-dropzone", cls="ai-chat-dropzone",
                    role="button", tabindex="0", onclick="celerpAiOpenFilePicker()",
                ),
                Div(id="ai-upload-error", cls="ai-upload-error", role="alert"),
                hx_post="/ai/chat",
                hx_target="#ai-messages",
                hx_swap="beforeend",
                hx_sync="this:drop",
                hx_on__before_request="celerpAiBeforeRequest();",
                hx_on__after_request="celerpAiAfterRequest(event);",
                cls="ai-input",
                id="ai-chat-form",
            ),
            Script(_chat_script(lang)),
            cls="ai-chat__main",
        ),
        cls="ai-chat",
    )


def _chat_script(lang: str = "en") -> str:
    text = json.dumps({
        "upload_failed": t("ai.upload_failed", lang),
        "upload_network": t("ai.upload_network_error", lang),
        "uploading": t("ai.uploading", lang),
        "remove_file": t("ai.remove_file", lang),
        "credits_remaining": t("ai.credits_remaining", lang),
        "quota_unavailable": t("ai.quota_unavailable", lang),
    })
    return "var CELERP_AI_TEXT = " + text + ";" + r"""
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

var CELERP_AI_UPLOADING = 0;
var CELERP_AI_REQUESTING = false;
var CELERP_AI_RECEIPT_FILES = {};

function celerpAiReceiptEligible(file) {
    return !!file && (
        file.type === 'image/jpeg' || file.type === 'image/png' ||
        file.type === 'image/webp' || file.type === 'application/pdf'
    );
}

function celerpAiRefreshComposer() {
    var form = document.getElementById('ai-chat-form');
    if (!form) return;
    var blocked = CELERP_AI_REQUESTING || CELERP_AI_UPLOADING > 0;
    form.classList.toggle('ai-input--busy', blocked);
    form.setAttribute('aria-busy', CELERP_AI_REQUESTING ? 'true' : 'false');
    var send = form.querySelector('.ai-input__send');
    var receipts = form.querySelector('.ai-input__receipts');
    var fileInput = document.getElementById('ai-file-input');
    var idsValue = document.getElementById('ai-file-ids').value;
    var ids = idsValue ? idsValue.split(',').filter(Boolean) : [];
    var receiptReady = ids.length > 0 && ids.every(function(id) {
        return CELERP_AI_RECEIPT_FILES[id] === true;
    });
    if (send) send.disabled = blocked;
    if (receipts) receipts.disabled = blocked || !receiptReady;
    if (fileInput) fileInput.disabled = blocked;
    var zone = document.getElementById('ai-chat-dropzone');
    if (zone) {
        zone.classList.toggle('ai-chat-dropzone--disabled', blocked);
        zone.setAttribute('aria-disabled', blocked ? 'true' : 'false');
    }
}

function celerpAiOpenFilePicker() {
    if (CELERP_AI_REQUESTING || CELERP_AI_UPLOADING > 0) return;
    var input = document.getElementById('ai-file-input');
    if (input) input.click();
}

function celerpAiBeforeRequest() {
    CELERP_AI_REQUESTING = true;
    celerpAiRefreshComposer();
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
    var receiptEligible = [];
    for (var i = 0; i < input.files.length; i++) {
        formData.append('files', input.files[i]);
        fileNames.push(input.files[i].name);
        receiptEligible.push(celerpAiReceiptEligible(input.files[i]));
    }
    _celerpAiUploadFormData(formData, fileNames, receiptEligible);
}

function celerpAiRemoveChip(btn, fileId) {
    delete CELERP_AI_RECEIPT_FILES[fileId];
    var chip = btn.closest('.ai-file-chip');
    if (chip) chip.remove();
    var fidsInput = document.getElementById('ai-file-ids');
    var ids = fidsInput.value ? fidsInput.value.split(',').filter(function(id) { return id !== fileId; }) : [];
    fidsInput.value = ids.join(',');
    var chips = document.getElementById('ai-file-chips');
    if (!chips.children.length) {
        document.getElementById('ai-chat-dropzone').classList.remove('ai-chat-dropzone--has-files');
    }
    celerpAiRefreshComposer();
}

function celerpAiResetForm() {
    document.getElementById('ai-query-input').value = '';
    document.getElementById('ai-file-ids').value = '';
    document.getElementById('ai-file-input').value = '';
    document.getElementById('ai-file-chips').innerHTML = '';
    CELERP_AI_RECEIPT_FILES = {};
    document.getElementById('ai-chat-dropzone').classList.remove('ai-chat-dropzone--has-files');
}

function celerpAiAfterRequest(event) {
    var typing = document.getElementById('ai-typing-indicator');
    if (typing) typing.remove();
    CELERP_AI_REQUESTING = false;
    var xhr = event && event.detail && event.detail.xhr;
    if (xhr && xhr.getResponseHeader('X-AI-Clear-Composer') === '1') celerpAiResetForm();
    celerpAiRefreshComposer();
    var m = document.getElementById('ai-messages');
    if (m) m.scrollTop = m.scrollHeight;
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
        if (CELERP_AI_REQUESTING || CELERP_AI_UPLOADING > 0) return;
        zone.classList.remove('ai-chat-dropzone--active');
        var dt = e.dataTransfer;
        if (dt && dt.files && dt.files.length) {
            // Trigger upload with the dropped files
            var fakeInput = document.getElementById('ai-file-input');
            // Build FormData directly from dropped files
            var formData = new FormData();
            var fileNames = [];
            var receiptEligible = [];
            for (var i = 0; i < dt.files.length; i++) {
                formData.append('files', dt.files[i]);
                fileNames.push(dt.files[i].name);
                receiptEligible.push(celerpAiReceiptEligible(dt.files[i]));
            }
            _celerpAiUploadFormData(formData, fileNames, receiptEligible);
        }
    });
    zone.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            celerpAiOpenFilePicker();
        }
    });
})();

// ── Reading-job progress from the notification stream ────────────────────────
document.addEventListener('celerp:batch-progress', function(e) {
    var d = e.detail || {};
    var el = document.querySelector('.ai-job[data-job-id="' + d.job_id + '"]');
    if (!el) return;
    var done = (d.completed || 0) + (d.failed || 0);
    var total = d.total || parseInt(el.dataset.total || '0', 10);
    var text = el.querySelector('.ai-job__text');
    if (text) text.textContent = (el.dataset.text || '').replace('{done}', done).replace('{total}', total);
    var bar = el.querySelector('.ai-job__bar');
    if (bar && total) bar.style.width = Math.round(done / total * 100) + '%';
    if (total && done >= total && window.htmx) htmx.trigger(el, 'celerp:job-done');
});

function _celerpAiUploadError(message) {
    var box = document.getElementById('ai-upload-error');
    if (box) box.textContent = message || '';
}

function _celerpAiUploadFormData(formData, fileNames, receiptEligible) {
    if (CELERP_AI_REQUESTING || CELERP_AI_UPLOADING > 0) return;
    CELERP_AI_UPLOADING += 1;
    celerpAiRefreshComposer();
    var zone = document.getElementById('ai-chat-dropzone');
    var chips = document.getElementById('ai-file-chips');
    _celerpAiUploadError('');

    var progressWrap = document.getElementById('ai-upload-progress');
    if (!progressWrap) {
        progressWrap = document.createElement('div');
        progressWrap.id = 'ai-upload-progress';
        progressWrap.className = 'ai-upload-progress';
        progressWrap.setAttribute('role', 'status');
        progressWrap.setAttribute('aria-live', 'polite');
        progressWrap.innerHTML = '<div class="ai-upload-progress__bar"></div><span class="ai-upload-progress__text"></span>';
        zone.appendChild(progressWrap);
    }
    progressWrap.style.display = 'flex';
    var bar = progressWrap.querySelector('.ai-upload-progress__bar');
    var ptext = progressWrap.querySelector('.ai-upload-progress__text');
    bar.style.width = '0%';
    ptext.textContent = CELERP_AI_TEXT.uploading;

    function finishUpload() {
        progressWrap.style.display = 'none';
        var fileInput = document.getElementById('ai-file-input');
        if (fileInput) fileInput.value = '';
        CELERP_AI_UPLOADING = Math.max(0, CELERP_AI_UPLOADING - 1);
        celerpAiRefreshComposer();
    }

    var xhr = new XMLHttpRequest();
    xhr.upload.addEventListener('progress', function(e) {
        if (e.lengthComputable) {
            var pct = Math.round((e.loaded / e.total) * 100);
            bar.style.width = pct + '%';
            ptext.textContent = pct + '%';
        }
    });
    xhr.addEventListener('load', function() {
        try {
            if (xhr.status >= 200 && xhr.status < 300) {
                var data = JSON.parse(xhr.responseText);
                if (data.file_ids) {
                    var existing = document.getElementById('ai-file-ids').value;
                    var all = existing ? existing.split(',').concat(data.file_ids) : data.file_ids;
                    document.getElementById('ai-file-ids').value = all.join(',');
                    zone.classList.add('ai-chat-dropzone--has-files');
                    data.file_ids.forEach(function(fid, idx) {
                        var fname = fileNames[idx] || fid;
                        CELERP_AI_RECEIPT_FILES[fid] = receiptEligible[idx] === true;
                        var chip = document.createElement('span');
                        chip.className = 'ai-file-chip';
                        chip.dataset.fileId = fid;
                        var name = document.createElement('span');
                        name.className = 'ai-file-chip__name';
                        name.textContent = fname;
                        var remove = document.createElement('button');
                        remove.type = 'button';
                        remove.className = 'ai-file-chip__remove';
                        remove.textContent = '✕';
                        remove.setAttribute('aria-label', CELERP_AI_TEXT.remove_file.replace('{name}', fname));
                        remove.addEventListener('click', function(e) {
                            e.stopPropagation();
                            celerpAiRemoveChip(remove, fid);
                        });
                        chip.appendChild(name);
                        chip.appendChild(remove);
                        chips.appendChild(chip);
                    });
                }
            } else {
                var detail = '';
                try { detail = JSON.parse(xhr.responseText).detail || ''; } catch (_) {}
                if (typeof detail !== 'string') detail = JSON.stringify(detail);
                _celerpAiUploadError(CELERP_AI_TEXT.upload_failed.replace('{detail}', detail));
            }
        } catch (_) {
            _celerpAiUploadError(CELERP_AI_TEXT.upload_network);
        } finally {
            finishUpload();
        }
    });
    xhr.addEventListener('error', function() {
        _celerpAiUploadError(CELERP_AI_TEXT.upload_network);
        finishUpload();
    });
    xhr.addEventListener('abort', finishUpload);
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
        if (data.unknown || data.disconnected) {
            badge.textContent = CELERP_AI_TEXT.quota_unavailable;
            return;
        }
        if (data.local || !data.paid) return;
        var remaining = data.remaining || 0;
        badge.textContent = CELERP_AI_TEXT.credits_remaining.replace('{n}', remaining);
        if (remaining < 10) {
            badge.classList.add('ai-quota--low');
        }
        if (remaining < 20 && link && data.topup_url) {
            link.href = data.topup_url;
            link.style.display = 'inline';
        }
    })
    .catch(function() {
        var badge = document.getElementById('ai-quota-display');
        if (badge) badge.textContent = CELERP_AI_TEXT.quota_unavailable;
    });
    celerpAiRefreshComposer();
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
    return data && data.paid === true;
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
