# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Company copies opened from a link, fetched in the background of this UI process.

Downloading a large copy can take minutes, so the request that receives the link only
starts the work and answers at once; the opening page then asks how far it has got.
An opening is known only by an unguessable id the browser holds in an HttpOnly cookie,
and only to the wizard entry point that started it. It ends staged (ready for the usual
preview, nothing imported), failed with the reason, or cancelled; finished openings are
forgotten after an hour.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

RUNNING, READY, FAILED = "running", "ready", "failed"
KEEP_SECONDS = 3600


class OpeningFailed(Exception):
    """The copy could not be opened; the message is shown to the owner as it is."""


@dataclass
class Opening:
    entry: str
    state: str = RUNNING
    stage: str = "requesting"
    received: int = 0
    total: int | None = None
    summary: dict | None = None
    message: str = ""
    finished_at: float | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def report(self, stage: str, received: int = 0, total: int | None = None) -> None:
        self.stage, self.received, self.total = stage, received, total

    @property
    def percent(self) -> int | None:
        """How much of the copy has arrived; None until its size is known."""
        if self.stage != "downloading" or not self.total:
            return None
        return min(100, self.received * 100 // self.total)


_openings: dict[str, Opening] = {}


def _sweep() -> None:
    cutoff = time.monotonic() - KEEP_SECONDS
    for key in [k for k, o in _openings.items() if o.finished_at is not None and o.finished_at < cutoff]:
        del _openings[key]


def start(entry: str, work: Callable[[Opening], Awaitable[dict]], unavailable: str) -> str:
    """Begin `work` in the background and return the opening's id. `work` returns the
    staged copy's summary or raises OpeningFailed; anything else is logged and shown
    as `unavailable`."""
    _sweep()
    key = secrets.token_urlsafe(32)
    opening = Opening(entry)

    async def run() -> None:
        try:
            opening.summary = await work(opening)
            opening.state = READY
        except OpeningFailed as exc:
            opening.state, opening.message = FAILED, str(exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Opening a company copy failed")
            opening.state, opening.message = FAILED, unavailable
        finally:
            opening.finished_at = time.monotonic()

    opening.task = asyncio.create_task(run())
    _openings[key] = opening
    return key


def find(key: str | None, entry: str) -> Opening | None:
    opening = _openings.get(key or "")
    return opening if opening is not None and opening.entry == entry else None


def forget(key: str | None, entry: str) -> None:
    """Stop the opening if it is still running and forget it."""
    opening = find(key, entry)
    if opening is None:
        return
    del _openings[key]
    if opening.task is not None and not opening.task.done():
        opening.task.cancel()
