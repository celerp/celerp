# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Streamed responses whose source is closed when the response ends."""

from __future__ import annotations

from starlette.responses import StreamingResponse


class ClosingStreamingResponse(StreamingResponse):
    """A StreamingResponse that closes its body iterator once the response ends.

    When the browser leaves mid-stream, Starlette stops sending but leaves the iterator
    suspended, so a stream proxied from the API kept its connection open until garbage
    collection. Closing the iterator here ends that connection at once."""

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self.body_iterator.aclose()
