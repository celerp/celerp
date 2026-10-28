"""Tests never connect off this machine: a socket left open to the live relay outlives
its test and fails a later one."""
import socket

import pytest


def test_a_connection_off_this_machine_is_refused_and_named(no_connection_off_this_machine):
    with socket.socket() as s:
        s.settimeout(5)
        with pytest.raises(ConnectionRefusedError):
            s.connect(("203.0.113.1", 443))
    with socket.socket() as s:
        s.settimeout(5)
        assert s.connect_ex(("203.0.113.2", 443)) != 0
    assert [address for address, _ in no_connection_off_this_machine] == [
        ("203.0.113.1", 443), ("203.0.113.2", 443)]
    no_connection_off_this_machine.clear()


@pytest.mark.asyncio
async def test_the_screens_ask_for_account_methods_without_reaching_the_relay():
    import ui.api_client as api

    assert await api.account_methods("token") == {
        "google": False, "free_email_quota": 0, "google_start_url": ""}
