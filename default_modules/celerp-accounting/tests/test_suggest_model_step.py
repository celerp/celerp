# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The bank suggestion model step against the model client's contract: its identity is the
request (company and key) plus a canonical hash of everything the model sees, and it is sent
through ``jobs.send_step``, so a step still running is asked again under the same identity."""

from __future__ import annotations

import copy
import json
import random
import uuid

import pytest

from celerp.ai import llm
from celerp.ai.llm import ModelResult, RelayError
from celerp_accounting import suggest

CO, KEY = uuid.uuid4(), "key-1"


def _call(company=CO, key=KEY) -> suggest.ModelCall:
    return suggest.ModelCall(company_id=company, user_id=uuid.uuid4(), key=key)


def _groups(rng: random.Random, n: int) -> list[dict]:
    return [{"key": f"g{i}", "direction": rng.choice(["in", "out"]), "count": rng.randint(1, 9),
             "total": f"{rng.randint(1, 9999)}.{rng.randint(0, 99):02d}",
             "sample": [f"LINE {i} {j}" for j in range(rng.randint(1, 3))],
             **({"client_said": f"said {i}"} if rng.random() < 0.3 else {})} for i in range(n)]


def _changed(rng: random.Random, groups: list[dict], accounts: list[str]):
    """One change to what the model sees, picked at random."""
    groups, accounts = copy.deepcopy(groups), list(accounts)
    g = rng.choice(groups)
    kind = rng.choice(["count", "total", "direction", "sample", "said", "drop", "add", "account"])
    if kind == "count":
        g["count"] += 1
    elif kind == "total":
        g["total"] = g["total"] + "1"
    elif kind == "direction":
        g["direction"] = "in" if g["direction"] == "out" else "out"
    elif kind == "sample":
        g["sample"] = g["sample"] + ["ONE MORE"]
    elif kind == "said":
        g["client_said"] = g.get("client_said", "") + " more"
    elif kind == "drop" and len(groups) > 1:
        groups.remove(g)
    elif kind == "account":
        accounts.append("9999")
    else:
        groups.append({**copy.deepcopy(g), "key": "new-group"})
    return groups, accounts


def test_the_step_identity_is_the_request_plus_the_canonical_model_input():
    """Seeded: the same groups and accounts in any order (and any key order inside a group) are
    one identity sent byte for byte the same; any change to what the model sees, or another
    company or key, is a new identity."""
    rng = random.Random(42916)
    for _trial in range(300):
        groups, accounts = _groups(rng, rng.randint(1, 6)), [str(rng.randint(1000, 9999)) for _ in range(4)]
        op, messages = suggest.model_step(_call(), groups, accounts)
        shuffled = [dict(rng.sample(list(g.items()), len(g))) for g in rng.sample(groups, len(groups))]
        same_op, same_messages = suggest.model_step(_call(), shuffled, rng.sample(accounts, len(accounts)))
        assert (same_op, same_messages) == (op, messages)
        new_groups, new_accounts = _changed(rng, groups, accounts)
        if (new_groups, new_accounts) != (groups, accounts):
            assert suggest.model_step(_call(), new_groups, new_accounts)[0] != op
        assert suggest.model_step(_call(key="key-2"), groups, accounts)[0] != op
        assert suggest.model_step(_call(company=uuid.uuid4()), groups, accounts)[0] != op


class _Client:
    """``llm.complete`` as the service answers it per operation id: ``running`` ids are
    refused operation_in_progress ``waits`` times, then answered; content sent before under
    an id must be sent again unchanged."""

    def __init__(self, waits: int = 0):
        self.waits, self.sent, self.kept = waits, [], {}

    async def complete(self, messages, *, operation_id, before_send, **_):
        from celerp.ai import jobs
        assert jobs.holding_slot() and jobs.sending_step()
        await before_send(1)
        self.sent.append(operation_id)
        if operation_id in self.kept and self.kept[operation_id] != messages:
            raise RelayError("operation_mismatch", "other content", status=422)
        self.kept[operation_id] = messages
        if self.waits:
            self.waits -= 1
            raise RelayError("operation_in_progress", "running", status=409, retry_after=0)
        groups = json.loads(messages[-1]["content"])["groups"]
        reply = {g["key"]: {"account_code": "5100", "reason": "r"} for g in groups}
        return ModelResult(message={"content": json.dumps(reply)}, model_used="m", usage={"credits": 1},
                           reservation_id=None, remaining=None, operation_credits=1)


@pytest.fixture
def client(monkeypatch):
    usage: list = []

    async def _record(session, **kw):
        usage.append(("send" if kw.get("send_bytes") else "settle", kw["operation_id"]))

    monkeypatch.setattr("celerp.ai.usage.record_usage", _record)
    made = _Client()
    monkeypatch.setattr(llm, "complete", made.complete)
    token = suggest.current_call.set(_call())
    yield made, usage
    suggest.current_call.reset(token)


@pytest.mark.asyncio
async def test_a_step_still_running_is_asked_again_under_its_identity_and_answered(client):
    made, usage = client
    made.waits = 3
    groups = _groups(random.Random(1), 2)
    reply = await suggest.ask_model(groups, ["5100"])
    assert set(reply) == {"g0", "g1"}
    assert len(made.sent) == 4 and len(set(made.sent)) == 1
    assert usage == [("send", op) for op in made.sent] + [("settle", made.sent[0])]


@pytest.mark.asyncio
async def test_a_retry_on_changed_input_is_a_new_step_and_an_identical_one_the_same(client):
    made, usage = client
    groups = _groups(random.Random(2), 3)
    await suggest.ask_model(groups, ["5100", "5200"])
    await suggest.ask_model(list(reversed(groups)), ["5200", "5100"])
    await suggest.ask_model(groups[1:], ["5100", "5200"])
    assert made.sent[0] == made.sent[1] != made.sent[2]
    assert usage == [(kind, op) for op in made.sent for kind in ("send", "settle")]


def test_the_model_reads_text_past_ascii_as_written_and_each_text_is_its_own_step():
    """A Thai sample reaches the model as written, never as escapes, and two samples that differ
    only past ASCII are two steps: the identity hashes every character the model sees."""
    groups = _groups(random.Random(3), 1)
    groups[0]["sample"] = ["ค่าเช่า มกราคม"]
    op, messages = suggest.model_step(_call(), groups, ["5100"])
    assert "ค่าเช่า มกราคม" in messages[1]["content"]
    groups[0]["sample"] = ["ค่าไฟ มกราคม"]
    assert suggest.model_step(_call(), groups, ["5100"])[0] != op
