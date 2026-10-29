"""The suite's own fixtures restore shared state (settings, the config file, module
and import state) on teardown. They must run after every patch a test made is undone,
so no autouse fixture may pull monkeypatch in ahead of them."""


def test_shared_state_is_restored_after_a_tests_patches_are_undone(request, monkeypatch):
    names = request.fixturenames
    restorers = [n for n in names if n.startswith(("_reset_", "_restore_"))]
    assert "_reset_cloud_settings" in restorers
    assert [n for n in restorers if names.index(n) > names.index("monkeypatch")] == []
