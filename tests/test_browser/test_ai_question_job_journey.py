# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Browser journeys for questions and reading jobs that run as jobs.

A question shows a pending bubble that polls its state and is swapped for the answer
when the job ends; past the promotion time it says it keeps working in the
background, with Stop. A reading job has Stop and, once stopped, Continue. After an
app restart an interactive question shows its retryable failure with Ask again.

The model is the scripted ``celerp.ai.service.complete`` of test_ai_chat_journey;
a step can be held on a threading.Event so the journey sees it while it runs. The
restart runs on the API server's own loop, captured from inside a model call.
"""
from __future__ import annotations

import asyncio
import threading

import pytest
from playwright.sync_api import Page, expect

from .test_ai_chat_journey import _SCRIPT, _answer, _open_chat, _send, agent_env  # noqa: F401
from .test_ai_documents_journey import _PNG, _attach, _send_receipts

pytestmark = pytest.mark.browser

_PROMOTED = ("This is taking longer than usual. It keeps working in the background, "
             "and you'll get a notification when it's done.")


class _Held:
    """A model reply that waits for ``release`` before it is returned."""

    def __init__(self, reply):
        self.reply = reply
        self.entered = threading.Event()
        self.released = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

    def release(self) -> None:
        self.released.set()


@pytest.fixture
def held_model(monkeypatch):
    import celerp.ai.service as ai_service
    real = ai_service.complete
    holds: list[_Held] = []

    async def _complete(messages, **kw):
        if holds and not holds[0].entered.is_set():
            h = holds[0]
            h.loop = asyncio.get_running_loop()
            h.entered.set()
            while not h.released.is_set():
                await asyncio.sleep(0.05)
            holds.pop(0)
            return h.reply
        return await real(messages, **kw)
    monkeypatch.setattr(ai_service, "complete", _complete)

    def _hold(reply) -> _Held:
        h = _Held(reply)
        holds.append(h)
        return h
    yield _hold
    for h in holds:
        h.release()


def _restart(loop: asyncio.AbstractEventLoop) -> None:
    from celerp.ai import jobs

    async def _go():
        await jobs.shutdown()
        await jobs.recover_on_start()
    asyncio.run_coroutine_threadsafe(_go(), loop).result(timeout=20)


def test_a_question_shows_a_pending_bubble_then_its_answer(page: Page, ui_server, held_model):
    held = held_model(_answer("Open Contacts and choose New."))
    _open_chat(page, ui_server)
    _send(page, "How do I add a supplier?")
    bubble = page.locator(".ai-msg--pending")
    expect(bubble).to_be_visible()
    assert held.entered.wait(10)
    held.release()
    expect(page.locator(".ai-msg--ai", has_text="Open Contacts and choose New.")).to_be_visible(timeout=10_000)
    expect(bubble).to_have_count(0)


def test_a_long_question_says_it_keeps_working_in_the_background_with_stop(page: Page, ui_server, held_model,
                                                                         monkeypatch):
    from celerp.ai import deadlines
    monkeypatch.setattr(deadlines, "PROMOTE_AFTER_S", 1.0)
    held = held_model(_answer("All done."))
    _open_chat(page, ui_server)
    _send(page, "Summarise this year's sales")
    assert held.entered.wait(10)
    bubble = page.locator(".ai-msg--pending")
    expect(bubble).to_contain_text(_PROMOTED, timeout=10_000)
    expect(bubble.get_by_role("button", name="Stop")).to_be_visible()
    held.release()
    expect(page.locator(".ai-msg--ai", has_text="All done.")).to_be_visible(timeout=10_000)


def test_a_reading_job_can_be_stopped_and_continued(page: Page, ui_server, held_model):
    held = held_model(_answer('{"document_kind": "receipt", "vendor_name": "Northwind"}'))
    _open_chat(page, ui_server)
    _attach(page, [{"name": f"r{i}.png", "mimeType": "image/png", "buffer": _PNG} for i in range(3)])
    _send_receipts(page, "Enter these as bills")
    job = page.locator(".ai-job")
    expect(job).to_be_visible()
    assert held.entered.wait(10)
    job.get_by_role("button", name="Stop").click()
    held.release()
    expect(job.get_by_role("button", name="Continue")).to_be_visible(timeout=10_000)
    job.get_by_role("button", name="Continue").click()
    expect(page.locator(".ai-job--done")).to_be_visible(timeout=20_000)


def test_an_interactive_question_interrupted_by_a_restart_offers_ask_again(page: Page, ui_server, held_model):
    held = held_model(_answer("Never shown."))
    _open_chat(page, ui_server)
    _send(page, "How many open invoices?")
    assert held.entered.wait(10)
    _restart(held.loop)
    error = page.locator(".ai-msg--error")
    expect(error).to_be_visible(timeout=10_000)
    expect(error.get_by_role("button", name="Ask again")).to_be_visible()
    _SCRIPT.append(_answer("You have 3 open invoices."))
    error.get_by_role("button", name="Ask again").click()
    expect(page.locator(".ai-msg--ai", has_text="You have 3 open invoices.")).to_be_visible(timeout=10_000)
