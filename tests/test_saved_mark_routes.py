# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A field on a document or list page counts as saved only when its save's answer carries the
saved mark. Every control of those pages that saves itself is found from the source and matched to
the app's routes: each such route is in the browser guard's table, answers a stored change with the
mark, and answers a refusal without it. A new self-saving control turns this red until its route
is in the guard."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

ROOT = Path(__file__).resolve().parents[1]
# The modules that draw document and list pages. A control in a shared component counts once its
# save goes to a document or list route.
_PAGE = ROOT / "ui/routes/documents.py"
_SOURCES = [_PAGE, *sorted((ROOT / "ui/components").glob("*.py"))]
# The keywords through which an HTML control saves: an htmx verb, or a script calling the list
# field save.
_HTML = {"hx_post": "POST", "hx_patch": "PATCH", "hx_put": "PUT", "onchange": "JS", "onblur": "JS", "oninput": "JS"}
_JS_SAVE = re.compile(r"_celerpPatchListField\(this, ['\"]([^'\"]+)['\"]")
_PARAM = re.compile("\x00(\\w+)\x00")  # a value a function is handed by its callers
# What a leading placeholder of a save URL stands for: the resource prefix, a record, its files.
_PREFIXES = ("/docs", "/lists", "/docs/Z", "/lists/Z", "/docs/Z/files", "/lists/Z/files")


def _own(fn: ast.AST):
    """The nodes of ``fn`` itself, not of the functions defined inside it."""
    todo = list(ast.iter_child_nodes(fn))
    while todo:
        node = todo.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(node))


def _params(fn: ast.AST) -> list[str]:
    a = fn.args
    return [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]


def _assigned(fn: ast.AST, name: str) -> list[ast.AST]:
    return [n.value for n in _own(fn) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None
            and any(isinstance(tg, ast.Name) and tg.id == name
                    for tg in (n.targets if isinstance(n, ast.Assign) else [n.target]))]


def _text(node: ast.AST, fn: ast.AST, seen: frozenset = frozenset()) -> list[str]:
    """The strings ``node`` can be, a placeholder ``{}`` for each value only known at run time. A
    name is followed to what the enclosing function assigns it (a parameter stands as itself, for
    the callers to fill in), and ``json.dumps(name)`` to it quoted."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name):
        if node.id in seen:
            return []
        own = [f"\x00{node.id}\x00"] if node.id in _params(fn) else []
        return own + [s for v in _assigned(fn, node.id) for s in _text(v, fn, seen | {node.id})]
    if isinstance(node, ast.IfExp):
        return _text(node.body, fn, seen) + _text(node.orelse, fn, seen)
    if isinstance(node, ast.BoolOp):
        return [s for v in node.values for s in _text(v, fn, seen)]
    if isinstance(node, ast.JoinedStr):
        out = [""]
        for part in node.values:
            if isinstance(part, ast.Constant):
                pieces = [part.value]
            elif (isinstance(part.value, ast.Call) and getattr(part.value.func, "attr", "") == "dumps"
                  and part.value.args):
                pieces = [f'"{s}"' for s in _text(part.value.args[0], fn, seen)] or ["{}"]
            else:
                pieces = ["{}"]
            out = [o + p for o in out for p in pieces]
        return out
    return []


def _forwarded(call: ast.Call, fn: ast.AST) -> bool:
    """Whether the call passes on the function's own ``**kwargs``: the function is then a control
    too, and its callers' keywords are what count."""
    own = getattr(getattr(fn, "args", None), "kwarg", None)
    return bool(own) and any(kw.arg is None and isinstance(kw.value, ast.Name) and kw.value.id == own.arg
                             for kw in call.keywords)


def _keywords(call: ast.Call, fn: ast.AST, order: list[str]) -> list[tuple[str, ast.AST]]:
    """The call's arguments by name (``order`` names the positional ones), with each ``**name``
    opened to the dicts the function builds."""
    out = list(zip(order, call.args))
    for kw in call.keywords:
        if kw.arg is not None:
            out.append((kw.arg, kw.value))
            continue
        if _forwarded(call, fn) and kw.value.id == fn.args.kwarg.arg:
            continue
        found = _assigned(fn, kw.value.id) if isinstance(kw.value, ast.Name) else [kw.value]
        assert found, f"line {call.lineno}: cannot open **{ast.unparse(kw.value)}"
        for d in (x for v in found for x in ([v.body, v.orelse] if isinstance(v, ast.IfExp) else [v])):
            if isinstance(d, ast.Dict):
                out += [(k.value, v) for k, v in zip(d.keys, d.values) if isinstance(k, ast.Constant)]
            elif isinstance(d, ast.Call) and getattr(d.func, "id", "") == "dict":
                out += [(k.arg, k.value) for k in d.keywords if k.arg]
    return out


def _saves(call: ast.Call, fn: ast.AST, keymap: dict, order: list[str]):
    """(verb, URL templates) for each save the call's control is given."""
    for key, value in _keywords(call, fn, order):
        verb = keymap.get(key)
        if verb == "JS":
            urls = [m for s in _text(value, fn) for m in _JS_SAVE.findall(s)]
            if urls:
                yield "PATCH", urls
        elif verb:
            yield verb, _text(value, fn)


def _functions():
    for path in _SOURCES:
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield path, fn, [n for n in _own(fn) if isinstance(n, ast.Call)]


def _control_saves():
    """(source line, verb, URL templates) for each control on these pages that saves itself. A
    function that hands a control its **kwargs, or a save URL it is given, is a control too."""
    functions = list(_functions())
    controls = {name: dict(_HTML) for name in ("Input", "Select", "Textarea")}
    order = {fn.name: _params(fn) for _, fn, _ in functions}
    changed = True
    while changed:
        changed = False
        for _, fn, calls in functions:
            for call in calls:
                name = getattr(call.func, "id", None)
                if name not in controls:
                    continue
                found = dict(controls[name]) if _forwarded(call, fn) else {}
                for verb, texts in _saves(call, fn, controls[name], order.get(name, []) if name not in _HTML_TAGS else []):
                    found |= {p: verb for s in texts for p in _PARAM.findall(s)}
                if not found.items() <= controls.get(fn.name, {}).items():
                    controls.setdefault(fn.name, {}).update(found)
                    changed = True
    for path, fn, calls in functions:
        for call in calls:
            name = getattr(call.func, "id", None)
            if name not in controls:
                continue
            for verb, texts in _saves(call, fn, controls[name], order.get(name, []) if name not in _HTML_TAGS else []):
                given = [s for s in texts if not _PARAM.search(s)]
                if given or not texts:  # a URL handed in is read at the caller
                    yield f"{path.name}:{call.lineno}", verb, given


_HTML_TAGS = {"Input", "Select", "Textarea"}


def _page_routes():
    from ui.app import app
    return [r for r in app.routes if getattr(r, "path", "").startswith(("/docs/", "/lists/"))]


def _routes_of(verb: str, templates: list[str], routes) -> set[tuple[str, str]]:
    found = set()
    for template in templates:
        rest = template.replace("{}", "Z")
        urls = [p + rest[1:] for p in _PREFIXES] if template.startswith("{}") else [rest]
        found |= {(verb, r.path) for r in routes for u in urls
                  if verb in (r.methods or ()) and r.path_regex.match(u)}
    return found


def _self_saving() -> set[tuple[str, str]]:
    routes = _page_routes()
    found: set = set()
    unmatched = []
    for where, verb, templates in _control_saves():
        assert templates, f"{where}: a control's save URL could not be read"
        hit = _routes_of(verb, templates, routes)
        if not hit and where.startswith(_PAGE.name):
            unmatched.append((where, verb, templates))
        found |= hit
    assert unmatched == [], f"controls that save to a route the app does not have: {unmatched}"
    return found


def test_every_self_saving_control_route_is_in_the_browser_guard():
    tree = ast.parse((Path(__file__).parent / "test_browser" / "test_saved_mark_guard.py").read_text())
    table = next(n.value for n in tree.body if isinstance(n, ast.AnnAssign) and n.target.id == "SCENARIOS")
    assert {ast.literal_eval(k) for k in table.keys} == _self_saving()


def test_the_enumeration_finds_a_new_self_saving_control():
    """A control added with a save of its own is found: the meta-test is not blind to new doors."""
    source = ("def cell(entity_id):\n    url = f'/lists/{entity_id}/field/x'\n"
              "    return Input(name='value', hx_put=url)\n")
    fn = ast.parse(source).body[0]
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "Input")
    assert list(_saves(call, fn, _HTML, [])) == [("PUT", ["/lists/{}/field/x"])]


DOC, LST = "doc:inv-1", "list:l-1"
_DOC = {"id": DOC, "doc_type": "invoice", "status": "draft", "currency": "USD", "version": 2,
        "line_items": [{"name": "Widget", "description": "Widget"}], "files": []}
_LIST = {"id": LST, "list_type": "quotation", "status": "draft", "version": 2, "line_items": []}

# For each self-saving route: the request, and the API call whose answer decides the outcome.
_REQUESTS = {
    ("PATCH", "/docs/{entity_id}/field/{field}"): ("patch", f"/docs/{DOC}/field/due_date", {"value": "2026-12-01"}, "patch_doc"),
    ("PATCH", "/docs/{entity_id}/line/{li_index}/field/{field}"): ("patch", f"/docs/{DOC}/line/0/field/description", {"value": "x"}, "patch_doc"),
    ("POST", "/docs/{entity_id}/field/{field}"): ("post", f"/docs/{DOC}/field/terms_text", {"terms_text": "x"}, "patch_doc"),
    ("POST", "/lists/{entity_id}/field/{field}"): ("post", f"/lists/{LST}/field/terms_text", {"terms_text": "x"}, "patch_list"),
    ("PATCH", "/lists/{entity_id}/field/{field}"): ("patch", f"/lists/{LST}/field/valid_until", {"value": "2026-12-01"}, "patch_list"),
    ("POST", "/lists/{entity_id}/line/{item_id}"): ("post", f"/lists/{LST}/line/item:1", {"counted_qty": "3"}, "set_audit_count"),
    ("PATCH", "/lists/{entity_id}/writeoff-line/{line_id}/{field}"): ("patch", f"/lists/{LST}/writeoff-line/w1/qty_out", {"value": "3"}, "set_writeoff_line"),
    ("POST", "/docs/{entity_id}/files/{file_id}/tag"): ("post", f"/docs/{DOC}/files/f1/tag", {"document_tag": "invoice"}, "tag_doc_file"),
    ("POST", "/docs/{entity_id}/share"): ("post", f"/docs/{DOC}/share", {"expires_at": "2026-12-31"}, "create_share_link"),
}


def test_every_self_saving_route_has_a_request_here():
    assert set(_REQUESTS) == _self_saving()


def _refusing():
    from ui.api_client import APIError
    return AsyncMock(side_effect=APIError(409, "Refused"))


async def _send(verb: str, url: str, form: dict, mocks: dict, signed_in: bool = True):
    from contextlib import ExitStack
    from ui.app import app as ui_app
    client = AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui")
    if signed_in:
        client.cookies.update({"celerp_token": make_test_token(role="owner")})
    mocks = {"get_doc": AsyncMock(return_value=_DOC), "get_list": AsyncMock(return_value=_LIST),
             "get_company": AsyncMock(return_value={"settings": {}, "timezone": "UTC"}), **mocks}
    with ExitStack() as stack:
        for name, mock in mocks.items():
            stack.enter_context(patch(f"ui.api_client.{name}", new=mock))
        return await getattr(client, verb)(url, data=form)


async def _answer(key, refused: bool):
    verb, url, form, decider = _REQUESTS[key]
    return await _send(verb, url, form, {decider: _refusing() if refused else AsyncMock(return_value={"version": 3})})


@pytest.mark.asyncio
@pytest.mark.parametrize("key", list(_REQUESTS), ids=lambda k: f"{k[0]} {k[1]}")
async def test_a_stored_change_carries_the_saved_mark_and_a_refusal_does_not(key):
    from ui.routes.documents import SAVED_MARK
    saved = await _answer(key, refused=False)
    assert saved.status_code < 300 and saved.headers.get(SAVED_MARK), (saved.status_code, saved.text[:200])
    refused = await _answer(key, refused=True)
    assert SAVED_MARK not in refused.headers, (refused.status_code, refused.text[:200])


_MEMO = {**_DOC, "doc_type": "memo", "status": "paid", "tax_rate": 0, "memo_fixed_amounts": {"tax": 5}}

# Every other answer a self-saving route gives that stores nothing: (verb, url, form, API mocks).
_NOT_STORED = {
    "a line field a final document keeps": ("patch", f"/docs/{DOC}/line/0/field/quantity", {"value": "2"}, {}),
    "a line no longer there": ("patch", f"/docs/{DOC}/line/5/field/description", {"value": "x"}, {}),
    "a new customer asked for on a document": ("patch", f"/docs/{DOC}/field/contact_id", {"value": "__new__"}, {}),
    "a tax rate on a document that cannot be read": (
        "patch", f"/docs/{DOC}/field/tax_rate", {"value": "7"}, {"get_doc": _refusing()}),
    "a memo rate that is not a number": (
        "patch", f"/docs/{DOC}/field/tax_rate", {"value": "seven"}, {"get_doc": AsyncMock(return_value=_MEMO)}),
    "a memo rate sent before its preview": (
        "patch", f"/docs/{DOC}/field/tax_rate", {"value": "7"}, {"get_doc": AsyncMock(return_value=_MEMO)}),
    "a memo rate the API refuses": (
        "patch", f"/docs/{DOC}/field/tax_rate", {"value": "7", "binding": "b", "idempotency_key": "k"},
        {"get_doc": AsyncMock(return_value=_MEMO), "set_memo_tax_rate": _refusing()}),
    "a list type the API refuses": (
        "patch", f"/lists/{LST}/field/list_type", {"value": "invoice"}, {"change_list_type": _refusing()}),
    "a new customer asked for on a list": ("patch", f"/lists/{LST}/field/contact_id", {"value": "__new__"}, {}),
    "a list customer the API refuses": (
        "patch", f"/lists/{LST}/field/contact_id", {"value": "contact:1"}, {"patch_list": _refusing()}),
    "a write-off field there is not": ("patch", f"/lists/{LST}/writeoff-line/w1/bogus", {"value": "x"}, {}),
    "a write-off quantity that is not a number": ("patch", f"/lists/{LST}/writeoff-line/w1/qty_out", {"value": "5x"}, {}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", list(_REQUESTS), ids=lambda k: f"{k[0]} {k[1]}")
async def test_a_save_from_a_signed_out_page_carries_no_saved_mark(key):
    from ui.routes.documents import SAVED_MARK
    verb, url, form, _ = _REQUESTS[key]
    answer = await _send(verb, url, form, {}, signed_in=False)
    assert SAVED_MARK not in answer.headers, (answer.status_code, answer.text[:200])


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_NOT_STORED))
async def test_an_answer_that_stores_nothing_carries_no_saved_mark(case):
    from ui.routes.documents import SAVED_MARK
    answer = await _send(*_NOT_STORED[case])
    assert SAVED_MARK not in answer.headers, (answer.status_code, answer.text[:200])
