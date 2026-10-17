# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A company copy link that can reach the client is always readable from here: its key is
kept on this computer before the link is emailed, whatever fails and wherever, and a new
link is made only for a copy whose key is still kept."""
from __future__ import annotations

import asyncio
import hashlib
import uuid

import httpx
import pytest

from celerp.services import company_handoff_sender as sender
from celerp.services.company_handoff_links import parse_handoff_link

FIRM, RECIPIENT = "Example Accounting", "client@example.com"


class Crash(Exception):
    """This installation stopping at one point of a send."""


class Relay:
    """The relay's copies for one company: one per operation, an email per distinct link,
    a send replayed when its operation and link were already sent. ``crash`` names the one
    call to fail: ``before:<call>`` fails it with nothing done, ``after:<call>`` does it
    and loses the answer."""

    def __init__(self, monkeypatch, company_id):
        self.company_id, self.rows, self.emails, self.crash = company_id, {}, [], None
        self.calls = []
        monkeypatch.setattr(sender.cloud_entitlement, "authenticated_request", self.request)
        real = httpx.AsyncClient
        monkeypatch.setattr(sender.httpx, "AsyncClient",
                            lambda **kw: real(transport=httpx.MockTransport(self.upload), **kw))

    def _fail(self, when, call):
        if self.crash == f"{when}:{call}":
            self.crash = None
            raise Crash(f"{when}:{call}")

    def _row(self, row):
        return {k: v for k, v in row.items() if k != "_op"}

    async def request(self, method, path, *, json=None, params=None, total_s, **kw):
        self.calls.append((method, path))
        if method == "GET":
            rows = [r for r in self.rows.values()
                    if not params.get("operation_id") or r["operation_id"] == params["operation_id"]]
            return httpx.Response(200, json={"items": [self._row(r) for r in rows]})
        if path == "/handoffs":
            self._fail("before", "create")
            row = self.rows.get(json["operation_id"])
            if row is None:
                copy_id = str(uuid.uuid4())
                row = self.rows[json["operation_id"]] = {
                    "id": copy_id, "link_id": copy_id, "company_id": str(self.company_id),
                    "operation_id": json["operation_id"], "firm_name": json["firm_name"],
                    "status": "pending", "stored": False, "can_restore": False,
                    "sent_at": None, "expires_at": "2026-10-31T08:00:00+00:00",
                    "opened": False, "downloaded": False, "company_opened": False}
            self._fail("after", "create")
            answer = {**self._row(row)}
            if row["status"] == "pending":
                answer["upload_url"] = (f"https://sfo2.digitaloceanspaces.com/backup/handoffs/"
                                        f"{uuid.UUID(row['id']).hex}.upload?X-Amz-Signature=synthetic")
            return httpx.Response(201, json=answer)
        copy_id = path.split("/")[2]
        row = next(r for r in self.rows.values() if r["id"] == copy_id)
        if path.endswith("/send"):
            self._fail("before", "send")
            if (json["operation_id"], json["link"]) not in self.emails:
                self.emails.append((json["operation_id"], json["link"]))
                row.update(status="sent", sent_at="2026-10-17T08:00:00+00:00")
            self._fail("after", "send")
            return httpx.Response(200, json={"sent": True, "id": copy_id})
        if path.endswith("/new-link"):
            row["link_id"] = str(uuid.uuid4())
            return httpx.Response(200, json={"link_id": row["link_id"], "expires_at": row["expires_at"]})
        return httpx.Response(200, json=self._row(row))

    def upload(self, req):
        self._fail("before", "upload")
        copy_id = req.url.path.rsplit("/", 1)[1].split(".")[0]
        row = next(r for r in self.rows.values() if uuid.UUID(r["id"]).hex == copy_id)
        row.update(status="ready", stored=True)
        self._fail("after", "upload")
        return httpx.Response(200)


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setattr(sender.files, "company_backups_dir", lambda: tmp_path / "copies")
    return tmp_path


def _send(company_id, operation_id, secret):
    async def export(destination):
        destination.write_bytes(b"synthetic books" * 400)
    return asyncio.run(sender.send_company_backup(
        export=export, company_id=company_id, operation_id=operation_id, secret=secret,
        firm_name=FIRM, recipient=RECIPIENT, reply_to="firm@example.com", note=""))


def _listed(company_id) -> dict:
    [copy] = asyncio.run(sender.company_copies(company_id))
    return copy


BOUNDARIES = ["before:create", "after:create", "before:upload", "after:upload", "before:send", "after:send"]


@pytest.mark.parametrize("crash", BOUNDARIES)
def test_a_send_stopped_at_any_point_leaves_every_emailed_link_readable(private, monkeypatch, crash):
    """The installation stops at one point of a send. Every link the relay emailed is
    listed here with its key, so Copy link and New link work in a new session; the same
    operation run again finishes with that same link and no second email."""
    company_id, operation, secret = uuid.uuid4(), uuid.uuid4(), bytes(range(32))
    relay = Relay(monkeypatch, company_id)
    relay.crash = crash
    with pytest.raises((Crash, sender.HandoffSendError)):
        _send(company_id, operation, secret)

    for _, link in relay.emails:
        copy = _listed(company_id)
        assert copy["link"] == link, (crash, copy)
        assert copy["renewable"] and copy["recipient"] == RECIPIENT, copy
        assert parse_handoff_link(link).proof_hash == hashlib.sha256(secret).hexdigest()

    sent = _send(company_id, operation, secret)  # a new session, the same operation
    assert len(relay.emails) == 1 and relay.emails[0][1] == sent["link"], relay.emails
    copy = _listed(company_id)
    assert (copy["link"], copy["renewable"]) == (sent["link"], True)
    assert not list(private.glob("copies/uploads/*.upload"))


def test_the_key_is_kept_before_the_link_is_emailed(private, monkeypatch):
    """At the moment the relay is asked to email the link, its key is already on this
    computer."""
    company_id, operation, secret = uuid.uuid4(), uuid.uuid4(), bytes(range(32))
    relay = Relay(monkeypatch, company_id)
    held_at_send = []
    request = relay.request

    async def watching(method, path, **kw):
        if path.endswith("/send"):
            copy_id = uuid.UUID(path.split("/")[2])
            held_at_send.append(sender.files.sent_copy(company_id, copy_id).get("key"))
        return await request(method, path, **kw)
    monkeypatch.setattr(sender.cloud_entitlement, "authenticated_request", watching)
    _send(company_id, operation, secret)
    assert held_at_send == [sender.encode_key(secret)]


def test_a_key_that_cannot_be_kept_sends_no_email(private, monkeypatch):
    """A disk that refuses the key stops the send before any email, with a reason; the
    same operation sends once the disk takes it."""
    company_id, operation, secret = uuid.uuid4(), uuid.uuid4(), bytes(range(32))
    relay = Relay(monkeypatch, company_id)
    real = sender.files.remember_sent_copy

    def full_disk(*args, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(sender.files, "remember_sent_copy", full_disk)
    with pytest.raises(sender.HandoffSendError) as refused:
        _send(company_id, operation, secret)
    assert relay.emails == [] and "Nothing was sent" in str(refused.value), refused.value

    monkeypatch.setattr(sender.files, "remember_sent_copy", real)
    sent = _send(company_id, operation, secret)
    assert [link for _, link in relay.emails] == [sent["link"]]


def test_another_key_for_a_sent_operation_is_refused(private, monkeypatch):
    """The same operation sent again with a key other than the one kept for its copy is
    refused: its link would not open the copy the client already has."""
    company_id, operation = uuid.uuid4(), uuid.uuid4()
    relay = Relay(monkeypatch, company_id)
    first = _send(company_id, operation, bytes(range(32)))
    with pytest.raises(sender.HandoffSendError, match="another key"):
        _send(company_id, operation, bytes(32))
    assert [link for _, link in relay.emails] == [first["link"]]
    assert _listed(company_id)["link"] == first["link"]


def test_no_new_link_for_a_copy_whose_key_is_not_kept(private, monkeypatch):
    """A stored copy this computer holds no key for is listed without New link, and New
    link is refused with what to do instead; the copy's current link is left alone."""
    company_id, operation, secret = uuid.uuid4(), uuid.uuid4(), bytes(range(32))
    relay = Relay(monkeypatch, company_id)
    sent = _send(company_id, operation, secret)
    copy_id = uuid.UUID(sent["id"])
    sender.files.forget_sent_copy(company_id, copy_id)
    copy = _listed(company_id)
    assert (copy["link"], copy["renewable"]) == (None, False), copy
    link_id = relay.rows[str(operation)]["link_id"]
    with pytest.raises(sender.HandoffRefused) as refused:
        asyncio.run(sender.new_link(company_id, copy_id, uuid.uuid4()))
    assert refused.value.status_code == 409 and "Resend" in str(refused.value), refused.value
    assert relay.rows[str(operation)]["link_id"] == link_id
    assert not [p for _, p in relay.calls if p.endswith("/new-link")]

    # A kept key that no longer reads as one is treated the same.
    sender.files.remember_sent_copy(company_id, copy_id, {"key": "not a key", "recipient": RECIPIENT})
    assert _listed(company_id)["renewable"] is False


def test_new_link_with_the_kept_key_opens_the_same_copy(private, monkeypatch):
    """The guard: with its key kept, New link replaces the link and the new one opens the
    same copy with the same key."""
    company_id, operation, secret = uuid.uuid4(), uuid.uuid4(), bytes(range(32))
    Relay(monkeypatch, company_id)
    sent = _send(company_id, operation, secret)
    renewed = asyncio.run(sender.new_link(company_id, uuid.UUID(sent["id"]), uuid.uuid4()))
    assert renewed["link"] and renewed["link"] != sent["link"]
    assert parse_handoff_link(renewed["link"]).proof_hash == hashlib.sha256(secret).hexdigest()
