"""Which roles read a module's ledger events: the permission comes only from a module this
process admitted (or a first-party copy) that is the sole claimant of the event prefix;
anything else falls to the company-admin default."""
from __future__ import annotations

import pytest

from celerp.modules import loader
from celerp.services.activity_redaction import can_read_event
from test_modules.test_admission import _clean_loader_state, _migrating_module, _modules, _uid  # noqa: F401


def _projecting(base, prefix: str, permission: str | None = "edit_documents", **extra):
    """An installed module projecting ``prefix``, its events read with ``permission``
    (no declaration when None)."""
    entry = {"prefix": prefix, "handler": "{inner}.proj:apply"}
    if permission is not None:
        entry["permission"] = permission
    return _migrating_module(base, f"acme-{_uid()}", base / "marker",
                             code={"proj.py": "def apply(state, event_type, data):\n    return state\n"},
                             slots={"projection_handler": [entry]}, **extra)


def _readers(event_type: str) -> set[str]:
    return {role for role in ("owner", "admin", "manager", "operator", "viewer")
            if can_read_event(event_type, {}, role)}


_DEFAULT_READERS = {"owner", "admin"}
_EDIT_DOCUMENT_READERS = {"owner", "admin", "manager", "operator"}


def test_a_sole_admitted_claimant_names_its_events_permission(_modules):
    pkg = _projecting(_modules, "acmea.")
    loader.load_all(str(_modules), {pkg.name})
    assert loader.event_read_permissions()["acmea."] == "edit_documents"
    assert _readers("acmea.order.placed") == _EDIT_DOCUMENT_READERS


def test_two_modules_claiming_one_prefix_grant_nothing(_modules):
    _projecting(_modules, "acmeb.", "view_inventory")
    _projecting(_modules, "acmeb.sub.", "view_inventory")
    assert not {"acmeb.", "acmeb.sub."} & set(loader.event_read_permissions())
    assert _readers("acmeb.sub.recorded") == _DEFAULT_READERS
    assert _readers("acmeb.other") == _DEFAULT_READERS


def test_a_module_refused_admission_grants_nothing(_modules):
    _projecting(_modules, "acmec.", "view_inventory", readable_when_off=True)
    assert loader.admit_modules(str(_modules), {p.name for p in _modules.iterdir()}).refused
    assert "acmec." not in loader.event_read_permissions()
    assert _readers("acmec.recorded") == _DEFAULT_READERS


def test_a_module_claiming_inside_a_core_prefix_grants_nothing(_modules):
    from celerp.modules import slots
    slots.register("projection_handler", {"prefix": "mpz.", "handler": "x.proj:apply",
                                          "permission": "view_inventory", "_module": "celerp-connectors"})
    _projecting(_modules, "mpz.order.", "edit_documents")
    granted = loader.event_read_permissions()
    assert granted.get("mpz.") is None and "mpz.order." not in granted
    assert _readers("mpz.order.placed") == _DEFAULT_READERS


def test_a_loaded_module_names_its_events_permission_without_being_admitted_again(_modules, monkeypatch):
    pkg = _projecting(_modules, "acmef.", "edit_documents")
    assert [m["name"] for m in loader.load_all(str(_modules), {pkg.name})] == [pkg.name]

    def _admitted_again(*_a, **_k):
        raise AssertionError("the module was admitted again")

    for name in ("admit_modules", "_admission_checks", "_check_dynamic_writes"):
        monkeypatch.setattr(loader, name, _admitted_again)
    assert loader.event_read_permissions()["acmef."] == "edit_documents"
    assert _readers("acmef.recorded") == _EDIT_DOCUMENT_READERS


def test_a_third_party_module_this_process_did_not_admit_names_nothing(_modules):
    _projecting(_modules, "acmeg.", "edit_documents")
    assert "acmeg." not in loader.event_read_permissions()
    assert _readers("acmeg.recorded") == _DEFAULT_READERS


def test_a_module_from_before_read_permissions_loads_and_its_events_stay_closed(_modules):
    # The projection_handler entry exactly as manifests wrote it before "permission" existed.
    pkg = _projecting(_modules, "acmed.", permission=None)
    loaded = loader.load_all(str(_modules), {pkg.name})
    assert [m["name"] for m in loaded] == [pkg.name] and not loader.load_errors()
    assert "acmed." not in loader.event_read_permissions()
    assert _readers("acmed.recorded") == _DEFAULT_READERS


@pytest.mark.parametrize("permission", ["", "no_such_permission", 7])
def test_a_projection_handler_with_an_unknown_read_permission_is_refused(permission):
    from celerp.modules import slots

    entry = {"prefix": "zz.", "handler": "celerp.projections.engine:_merge", "_module": "zz",
             "_first_party": False, "permission": permission}
    with pytest.raises(ValueError):
        slots.check("projection_handler", entry)


def test_a_projection_handler_may_leave_its_read_permission_out():
    from celerp.modules import slots

    slots.check("projection_handler", {"prefix": "zz.", "handler": "celerp.projections.engine:_merge",
                                       "_module": "zz", "_first_party": False})
