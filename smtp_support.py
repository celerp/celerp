# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A real SMTP server on localhost for email tests, shared by the core and module tests.

It speaks enough SMTP for aiosmtplib (EHLO, AUTH, MAIL, RCPT, DATA, QUIT), keeps every
message it accepted, and can be told to answer one step with a refusal or to drop the
connection once the message has been handed over, so a test sees the transport's real
outcome instead of a mocked exception."""

from __future__ import annotations

import asyncio
import contextlib
from email import message_from_bytes
from email.message import Message
from unittest.mock import patch


class SmtpStub:
    """``refuse``: the command (``"MAIL"``, ``"RCPT"``, ``"DATA"``, ``"AUTH"``) answered 550,
    or ``"END"`` for the reply after the message. ``drop_after_message``: close the connection
    once the message is received, before replying."""

    def __init__(self, *, refuse: str | None = None, drop_after_message: bool = False):
        self.refuse, self.drop_after_message = refuse, drop_after_message
        self.messages: list[Message] = []
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def __aenter__(self) -> "SmtpStub":
        self._server = await asyncio.start_server(self._session, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async def say(line: str) -> None:
            writer.write(line.encode() + b"\r\n")
            await writer.drain()

        try:
            await say("220 stub ready")
            while line := await reader.readline():
                verb = line.decode().strip().split(" ", 1)[0].upper()
                if verb == self.refuse:
                    await say("550 refused")
                elif verb in ("EHLO", "HELO"):
                    await say("250-stub")
                    await say("250 AUTH PLAIN LOGIN")
                elif verb == "AUTH":
                    await say("235 accepted")
                elif verb == "DATA":
                    await say("354 go ahead")
                    body = b""
                    while (chunk := await reader.readline()) not in (b".\r\n", b""):
                        body += chunk[1:] if chunk.startswith(b"..") else chunk
                    if self.drop_after_message:
                        return
                    if self.refuse == "END":
                        await say("554 rejected")
                        continue
                    self.messages.append(message_from_bytes(body))
                    await say("250 queued")
                elif verb == "QUIT":
                    await say("221 bye")
                    return
                else:
                    await say("250 ok")
        except ConnectionError:
            pass
        finally:
            writer.close()


@contextlib.contextmanager
def smtp_settings(port: int, **overrides):
    """Point the app's SMTP settings at ``port`` on localhost, with no Cloud connection."""
    values = {"gateway_token": "", "smtp_host": "127.0.0.1", "smtp_port": port, "smtp_user": "user",
              "smtp_password": "pass", "smtp_from": "noreply@example.com", "smtp_tls": False, **overrides}
    with contextlib.ExitStack() as stack:
        for name, value in values.items():
            stack.enter_context(patch(f"celerp.config.settings.{name}", value))
        yield
