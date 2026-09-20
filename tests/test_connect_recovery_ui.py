# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
from pathlib import Path
from fasthtml.common import to_xml


def test_disconnected_token_bound_tab_renders_connect_surface():
    from ui.routes.settings import _cloud_relay_tab
    html = to_xml(_cloud_relay_tab(
        relay_status="inactive", public_url="", tier="free",
        token_bound=True, entitlement_known=True, disconnected=True))
    assert "cloud-connect-btn" in html
    assert "cloud-disconnect" not in html


def test_full_page_and_fragment_preserve_disconnect_state():
    source = Path("ui/routes/settings_cloud.py").read_text()
    assert "gw_ok = (not disconnected and (" in source
    assert "disconnected=disconnected" in source


def test_account_poll_uses_semantic_activation_intent():
    source = Path("ui/routes/account.py").read_text()
    assert 'intent=("connect" if panel_id == "cloud-relay-tab"' in source
    assert 'else "account")' in source


def test_claim_forms_carry_account_vs_connect_intent():
    account = Path("ui/routes/account.py").read_text()
    settings = Path("ui/routes/settings.py").read_text()
    assert 'name="connect_intent"' in account
    assert 'name="connect_intent"' in settings
    assert '"intent": connect_intent' in settings


def test_build_checkout_has_history_for_semver_derivation():
    workflow = Path(".github/workflows/build.yml").read_text()
    step = workflow.index("- name: Set Electron version from git tag or development commit")
    checkout = workflow.rfind("- uses: actions/checkout@v4", 0, step)
    assert checkout >= 0
    assert "fetch-depth: 0" in workflow[checkout:step]
