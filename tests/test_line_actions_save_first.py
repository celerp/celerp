# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every action a draft page with editable lines offers whose server side reads the stored lines
(Finalize, Issue, Write off stock, Duplicate, Convert, Send) saves the page's pending line edits
first, through the same path as Finalize (persist_then_post). Every other posting control on such a
page is named here with the reason it does not read the lines, so a new button fails until decided.
Pages render with every optional feature on (sharing, email, online payment), so a control behind
a setting is decided too."""
from __future__ import annotations

import ast
import re
from pathlib import Path

from celerp.services.list_behavior import LIST_BEHAVIOR
from ui.routes.documents import _DOC_TYPES, _doc_detail, persist_then_post

# Server actions that read the stored lines.
_ON_LINES = ("/action/finalize", "/action/write-off", "/action/duplicate", "/convert", "/action/send")
# Posting controls that never read the lines: a status change, deleting the record, a note, and the
# header text fields (customer note, terms), which save on their own.
_NOT_ON_LINES = ("/action/mark_sent", "/action/delete", "/notes", "/field/customer_note", "/field/terms_text")
_DIRECT = ("hx_post", "hx-post", "hx_confirm", "hx-confirm")
_LINES = [{"line_id": "l1", "name": "Row", "description": "Row", "quantity": 1, "unit_price": 1, "line_total": 1}]
_WRITE_OFF = LIST_BEHAVIOR["writeoff"].terminal[0]
_FEATURES_ON = {"share_enabled": True, "share_active": True, "payments_on": True, "free_send_offer": True,
                "email_quota": 10}


def _nodes(items):
    for node in items:
        if hasattr(node, "attrs"):
            yield node
            yield from _nodes(node.children)


def _drafts():
    for list_type in LIST_BEHAVIOR:
        yield {"entity_id": "list:L-1", "doc_type": "list", "list_type": list_type, "status": "draft",
               "line_items": _LINES, "currency": "USD"}
    for doc_type in [d for d in _DOC_TYPES if d != "list"] + ["quotation"]:
        yield {"entity_id": "doc:D-1", "doc_type": doc_type, "status": "draft", "line_items": _LINES, "currency": "USD"}


def _posts(doc: dict, role: str = "owner") -> list[tuple[str, object]]:
    """(url, node) for every control on the rendered page that posts."""
    out = []
    for node in _nodes([_doc_detail(doc, role=role, **_FEATURES_ON)]):
        url = node.attrs.get("hx-post") or node.attrs.get("hx_post")
        js = node.attrs.get("onclick") or node.attrs.get("onsubmit") or ""
        if not url and "_celerpPostOnLines(" in js:
            url = js.split("_celerpPostOnLines('", 1)[1].split("'", 1)[0]
        if url:
            out.append((url, node))
    return out


def test_every_action_on_the_lines_saves_them_first():
    found = set()
    for doc in _drafts():
        for url, node in _posts(doc):
            on_lines = url.endswith(_ON_LINES)
            assert on_lines or url.endswith(_NOT_ON_LINES), f"undecided posting control {url} on {doc['doc_type']}"
            if not on_lines:
                continue
            found.add(url.rsplit("/", 1)[-1])
            assert not set(node.attrs) & set(_DIRECT), node
            confirm = _WRITE_OFF.confirm if url.endswith(f"/action/{_WRITE_OFF.key}") else None
            if node.tag == "form":
                assert node.attrs.get("onsubmit") == persist_then_post(url, confirm, form=True), node
            else:
                assert node.attrs.get("onclick") == persist_then_post(url, confirm), node
    assert found == {u.rsplit("/", 1)[-1] for u in _ON_LINES}


def test_write_off_asks_before_it_saves():
    assert '"' + _WRITE_OFF.confirm + '"' in persist_then_post("/lists/list:L-1/action/write-off", _WRITE_OFF.confirm)


def test_without_editable_lines_the_actions_post_directly():
    writeoff = next(d for d in _drafts() if d.get("list_type") == "writeoff")
    posts = dict(_posts(writeoff, role="viewer"))
    button = posts["/lists/list:L-1/action/write-off"]
    assert button.attrs.get("hx_confirm", button.attrs.get("hx-confirm")) == _WRITE_OFF.confirm
    assert "onclick" not in button.attrs


# The page's line actions stop on a line edit not stored. A line cell save that stored nothing marks
# its answer data-cell-unsaved, and that mark is the only thing the page reads for it: nothing else
# sets it and no styling class stands in for it, so a hint can never stop an action.
_ROOT = Path(__file__).resolve().parents[1]
_SOURCES = [p for d in ("ui", "default_modules") for p in (_ROOT / d).rglob("*") if p.suffix in (".py", ".js")]
_DOCS = _ROOT / "ui" / "routes" / "documents.py"


def _function(source: str, head: str) -> str:
    """The text of the function opening with *head*: a page script function up to its closing brace,
    a module-level def up to the next line at module level."""
    start = source.index(head)
    end = re.compile(r"\n\}\}\n" if "function" in head else r"\n\S").search(source, start + len(head))
    return source[start:end.end()]


def _enclosing_def(source: str, at: int) -> str:
    return re.findall(r"^\s*(?:async )?def (\w+)", source[:at], re.M)[-1]


def test_only_a_line_cell_save_sets_the_unsaved_mark():
    docs = _DOCS.read_text()
    for path in _SOURCES:
        text = path.read_text(errors="ignore")
        if path != _DOCS:
            assert not re.search(r"data[-_]cell[-_]unsaved|cellUnsaved", text), path
    assert docs.count("data_cell_unsaved") == 1 and "cellUnsaved" not in docs
    assert "data_cell_unsaved" in _function(docs, "def _cell_save_refusal(")
    calls = [m.start() for m in re.finditer(r"(?<!def )_cell_save_refusal\(", docs)]
    assert {_enclosing_def(docs, at) for at in calls} == {"list_writeoff_line_save"}
    # The page only reads the mark (the unsaved check, and the cell save telling a refusal apart);
    # no script sets it.
    assert docs.count("querySelector('[data-cell-unsaved]')") == 2
    assert not re.search(r"(set|toggle)Attribute\(\s*'data-cell-unsaved", docs)


def test_the_unsaved_check_reads_the_mark_not_a_styling_class():
    check = _function(_DOCS.read_text(), "function _celerpLineCellsUnsaved()")
    assert "[data-cell-unsaved]" in check
    assert not re.search(r"\.cell-error|cell-error'|classList", check), check


# Every refusal of a save on the page shows through one function, on the save status and in a toast,
# so it reaches the user wherever the save started (a dialog over the page included).
def test_one_function_writes_every_save_refusal():
    docs = _DOCS.read_text()
    writer = _function(docs, "function _celerpSaveRefused(text)")
    assert "getElementById('save-status')" in writer and "celerpToast(text, 'error')" in writer
    persist = _function(docs, "async function _celerpPersistOnce()")
    for path in _SOURCES:
        text = path.read_text(errors="ignore")
        for m in re.finditer(r"getElementById\(['\"]save-status|#save-status|id=['\"]save-status", text):
            line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.start())]
            if path == _DOCS and (writer.find(line) >= 0 or persist.find(line) >= 0
                                  or 'id="save-status"' in line):
                continue
            if path.name == "shell.py" and line.startswith("var _CELERP_NOTICE"):
                continue
            raise AssertionError(f"{path}: {line.strip()}")
    # The rows save writes only its success mark itself; every refusal goes through the writer.
    assert "_celerpSaveRefused(msg)" in persist
    assert not re.search(r"2717|'red'|celerpToast\(", persist), persist


# ── Handler strings (B1 class guard) ───────────────────────────────────────
# Every click or submit handler that posts to an action route is built by persist_then_post; a
# handler string that posts to an action route itself would run the action on lines the page has
# not stored.
_HANDLERS = {"onclick", "onsubmit"}
_SAVES_FIRST = "persist_then_post"


def _ui_sources() -> list[Path]:
    return sorted({*_ROOT.glob("ui/**/*.py"), *_ROOT.glob("default_modules/*/*/ui*.py"),
                   *_ROOT.glob("default_modules/*/*/ui/**/*.py")})


def _literal_text(node) -> str:
    """The literal text a string expression holds (f-string and + parts joined)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_literal_text(v) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_text(node.left) + _literal_text(node.right)
    return ""


def _posts_to_an_action(text: str) -> bool:
    compact = text.replace(" ", "").replace('"', "'")
    return "/action/" in text and ("htmx.ajax('POST'" in compact or "hx-post" in compact or "fetch(" in compact)


def _violations(source: str, path: str = "<snippet>") -> list[str]:
    tree = ast.parse(source)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _SAVES_FIRST:
            continue
        if isinstance(node, ast.keyword) and node.arg in _HANDLERS:
            value = node.value
            if isinstance(value, ast.Call) and getattr(value.func, "id", getattr(value.func, "attr", "")) == _SAVES_FIRST:
                continue
            if "/action/" in _literal_text(value):
                out.append(f"{path}:{node.arg} posts to an action route without saving the lines first")
        elif isinstance(node, (ast.Constant, ast.JoinedStr)) and _posts_to_an_action(_literal_text(node)):
            out.append(f"{path}: a handler string posts to an action route itself")
    return out


def test_line_actions_save_the_lines_first():
    found = []
    for path in _ui_sources():
        found += _violations(path.read_text(encoding="utf-8"), str(path.relative_to(_ROOT)))
    assert not found, "\n".join(found)


def test_the_rule_catches_an_action_posted_directly():
    """Self-check: the rule fails a handler that posts to an action route itself."""
    assert _violations('Button("Go", onclick="htmx.ajax(\'POST\',\'/docs/x/action/y\',{swap:\'none\'})")')
    assert _violations('Button("Go", onclick=f"htmx.ajax(\'POST\',\'/lists/{eid}/action/finalize\')")')
    assert not _violations('Button("Go", onclick=persist_then_post(f"/lists/{eid}/action/finalize"))')
