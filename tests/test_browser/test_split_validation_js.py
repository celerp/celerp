"""The split form's quantity check lets a valid split through and stops a bad one.

The split modal is redrawn by HTMX, so its script runs again on each open: the document
listeners must stay installed once, or every submit is checked once per open."""
import pytest

from ui.routes.inventory import _BULK_SPLIT_JS

pytestmark = pytest.mark.browser

_FORM = """<form data-validates-split data-invalid-qty="Enter a quantity above zero">
  <input name="child_qty" value="{a}"><input name="child_qty" value="{b}"></form>"""

_SUBMIT = """() => {
  window._checks = 0;
  const check = window.validateSplitInputs;
  window.validateSplitInputs = (f) => { window._checks += 1; return check(f); };
  const ev = new Event('submit', {bubbles: true, cancelable: true});
  document.querySelector('form').dispatchEvent(ev);
  return {prevented: ev.defaultPrevented, checks: window._checks,
          message: document.querySelector('[name=child_qty]').validationMessage};
}"""


def _submit(playwright, a, b, opens=2):
    browser = playwright.chromium.launch()
    try:
        page = browser.new_page()
        page.set_content(_FORM.format(a=a, b=b))
        for _ in range(opens):
            page.add_script_tag(content=_BULK_SPLIT_JS)
        return page.evaluate(_SUBMIT)
    finally:
        browser.close()


def test_a_valid_split_submits_and_is_checked_once(playwright):
    assert _submit(playwright, "2", "3") == {"prevented": False, "checks": 1, "message": ""}


def test_a_split_with_a_zero_quantity_is_stopped_with_the_reason(playwright):
    result = _submit(playwright, "0", "3")
    assert result["prevented"] is True
    assert result["message"] == "Enter a quantity above zero"
