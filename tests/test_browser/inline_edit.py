"""Inline cell editing for browser tests.

Single source for the double-click, edit, commit, settle sequence. The recipe
and worksheet tables both render their cells with the shared `edit_cell`
component (ui/components/table.py), so they open, cancel and save identically
and one helper serves both.

Two races made these tests flaky. Both are handled here, by removing the race
rather than by widening a timeout:

Committing a cell fires an HTMX patch, swaps the cell back to its display form
and then re-renders the whole section (ui/routes/inventory.py:6116). The API
confirms the new value before those swaps land, so opening the next editor
immediately can double-click a row that is being replaced. The editor lookup
then finds the previous cell's input, the edit goes to a detached node, nothing
is saved, and the next wait times out. Settling before and after every edit
closes that window.

A retry then has to start from a clean slate. An attempt that fails after its
double-click leaves an editor open, and an open editor makes every later settle
fail, so a naive retry never gets past its first step: it reports a settle
timeout and the real failure is lost. The retry cancels the stray editor with
Escape first, which the app handles by restoring the cell (GDR 2j).
"""
from __future__ import annotations

EDITOR = "[name=value]"
SETTLE_TIMEOUT = 8000


def settled(page, scope):
    """Wait until no cell editor is open inside `scope`."""
    page.wait_for_function(
        f"!document.querySelector('{scope} {EDITOR}')",
        timeout=SETTLE_TIMEOUT,
    )


def cancel_editor(page, scope):
    """Escape out of an editor a failed attempt left open, so a retry can settle."""
    editor = page.locator(f"{scope} {EDITOR}").last
    if editor.count() == 0:
        return
    editor.press("Escape")
    settled(page, scope)


def set_cell(page, col, value, *, scope, kind="text", confirm=False):
    """Double-click a cell, edit it, commit, and wait for the swap to settle.

    kind: "text" commits on Enter, "select" on change, "textarea" on blur
          (Enter would insert a newline there, not save).
    confirm: require the committed cell to show `value` before returning. Use it
             where the cell displays the value verbatim; a select showing a
             translated label does not.
    """
    for attempt in range(2):
        try:
            settled(page, scope)
            page.dblclick(f'{scope} td[data-col="{col}"]')
            if kind == "select":
                el = page.locator(f"{scope} select[name=value]").last
                el.wait_for(state="visible", timeout=4000)
                el.select_option(str(value))
            elif kind == "textarea":
                el = page.locator(f"{scope} textarea[name=value]").last
                el.wait_for(state="visible", timeout=4000)
                el.fill(str(value))
                el.blur()
            else:
                el = page.locator(f"{scope} input[name=value]").last
                el.wait_for(state="visible", timeout=4000)
                el.fill(str(value))
                el.press("Enter")
            settled(page, scope)
            if confirm:
                cell = page.locator(f'{scope} td[data-col="{col}"]')
                cell.wait_for(state="visible", timeout=6000)
                shown = cell.inner_text()
                if str(value) not in shown:
                    raise AssertionError(f'{col} shows "{shown}" after saving "{value}"')
            return
        except Exception:
            if attempt:
                raise
            cancel_editor(page, scope)
