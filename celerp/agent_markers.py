# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The markers a route or a dependency carries for the assistant's tool compiler.

Every route is an assistant tool unless its own decorator carries
``openapi_extra=agent_excluded(<reason>)``. A dependency that refuses a caller for a
reason require_permission does not express (the installation owner, the company owner)
is tagged with ``agent_gate`` where it is defined, so compile drops the tool for a caller
who fails it instead of failing only when run. Kept free of imports, so any route file
and the auth services that define gates can import it at load time.
"""

from __future__ import annotations

from typing import Any

AGENT_GATES = frozenset({"owner", "install_owner"})


def agent_gate(dependency: Any, rule: str) -> Any:
    """Tag ``dependency`` with ``rule`` and return it."""
    if rule not in AGENT_GATES:
        raise ValueError(f"unknown agent gate: {rule!r}")
    dependency.agent_gate = rule
    return dependency


# Why a route is not an assistant tool.
_NOT_USER_ACTIONS: dict[str, str] = {
    "session": "Sign-in, first-boot and session plumbing: runs before or around a signed-in user, "
               "never as an action inside a company.",
    "internal": "An endpoint the app calls by itself (health probe, live-update stream, image source, "
                "call-to-action fragment), not an action a user takes.",
    "public": "A page served to an outside party who opens a shared link, with no signed-in user.",
    "machine": "Called by another program (a linked company or a connector), never by a person.",
}
# User actions kept off the assistant: the operator prompt names each, so the model
# points the user to the screen instead.
USER_ACTION_EXCLUSIONS: dict[str, str] = {
    "secret": "Takes a password, one-time code, claim token or service key; typed into the chat it "
              "would go to the AI provider and stay in the conversation log.",
    "claim_step": "Sends a one-time code to an email address to start a claim; the code it sends "
                  "only works through the next step, which takes the code and stays off as secret, "
                  "so offering this one alone would lead nowhere.",
    "company": "Replaces or erases the whole company, or the installation's data, that the "
               "conversation and its confirmation run in.",
    "switch": "Opens a different company and signs the person in to it: the answer is a sign-in "
              "token, which in the chat would go to the AI provider, and the conversation stays behind.",
    "no_company": "Serves a login that has no company left; the assistant runs inside a company, so "
                  "it is never there to take this step.",
    "installation": "Restarts the server, which stops the assistant before it can record what happened.",
    "assistant": "The assistant's own controls and chat plumbing: confirming, dismissing, rerunning or "
                 "stopping its proposals and runs, starting or deleting a conversation, attachments; "
                 "as tools they would let it approve its own proposals or erase the conversation it runs in.",
}
AGENT_EXCLUSIONS: dict[str, str] = {**_NOT_USER_ACTIONS, **USER_ACTION_EXCLUSIONS}


def agent_excluded(reason: str) -> dict[str, str]:
    """The ``openapi_extra`` that keeps a route off the assistant, with the reason why."""
    if reason not in AGENT_EXCLUSIONS:
        raise ValueError(f"unknown agent exclusion reason: {reason!r}")
    return {"x-celerp-agent-excluded": reason}
