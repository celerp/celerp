# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Company handoff links may carry a secret but never arbitrary destinations."""
import hashlib
import uuid

import pytest

from celerp.services.company_handoff_links import handoff_page_link, parse_handoff_link


def test_email_and_desktop_link_round_trip():
    hid = uuid.uuid4()
    key = bytes(range(32))
    email = handoff_page_link(hid, key)
    parsed = parse_handoff_link(email)
    assert parsed.copy_id == hid
    assert parsed.secret == key
    assert parsed.proof_hash == hashlib.sha256(key).hexdigest()
    assert key.hex() not in repr(parsed)
    deep_link = f"celerp://handoff/{hid}#{email.split('#', 1)[1]}"
    assert parse_handoff_link(deep_link) == parsed


@pytest.mark.parametrize("mutate", [
    lambda u: u.replace("https://", "http://"),
    lambda u: u.replace("relay.celerp.com", "evil.example"),
    lambda u: u.replace("relay.celerp.com", "relay.celerp.com@evil.example"),
    lambda u: u.replace("relay.celerp.com", "relay.celerp.com:8443"),
    lambda u: u.replace("/open#", "/open?key=secret#"),
    lambda u: u.replace("/open#", "/other#"),
    lambda u: u.replace("/open#", "/open/#"),
    lambda u: u + "/extra",
    lambda u: u[:-1] + "%",
    lambda u: u.split("#", 1)[0] + "#",
    lambda u: u.split("#", 1)[0] + "#" + "A" * 43 + "A",
])
def test_invalid_urls_fail_closed(mutate):
    email = handoff_page_link(uuid.uuid4(), bytes(range(32)))
    with pytest.raises(ValueError, match="Invalid company copy link"):
        parse_handoff_link(mutate(email))


def test_noncanonical_fragment_rejected():
    email = handoff_page_link(uuid.uuid4(), bytes(range(32)))
    # Last base64 character has unused pad bits for a 32-byte secret.
    corrupted = email[:-1] + "h" if email[-1] != "h" else email[:-1] + "i"
    with pytest.raises(ValueError):
        parse_handoff_link(corrupted)


def test_no_arbitrary_keys_or_uris():
    with pytest.raises(ValueError):
        handoff_page_link(uuid.uuid4(), b"short")
    with pytest.raises(ValueError):
        parse_handoff_link("celerp://handoff/not-a-uuid#" + "A" * 43)
