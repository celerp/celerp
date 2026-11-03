"""Every refusal a route the AI calls can answer with reads in the reader's language.

The AI calls the app's own routes (those marked ``x-celerp-agent``) and keeps what a
refusing route answered, to show the person who confirmed the action, then and after a
reload. So a refusal those routes raise, or any function they call raises, is keyed
where it is raised (celerp.accounting_roles.refusal, or keyed_error), or raised as
``Refused`` with its catalog key. This walks the routes, every handler a module adds to a
slot and every exception handler the app answers with, and their callees, and fails on
any other detail."""
from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import json
import re
import textwrap
import types
import typing
from pathlib import Path

from fastapi import HTTPException
from starlette.responses import Response

from celerp.accounting_roles import Refused, catalog_refusal, refusal
from celerp.ai.tools import _api_routes, keyed_error
from celerp.error_body import error_response
from ui import i18n


_ROOT = Path(__file__).resolve().parents[1]
# The English catalog as the app merges it: the central file and every bundled module's own.
_EN = {k: v for path in (Path(i18n.__file__).parent / "locales" / "en.json",
                         *_ROOT.glob("default_modules/*/*/locales/en.json"))
       for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
# The canonical makers of a keyed refusal, and the index of the catalog key they take.
_MAKERS = {refusal: 0, catalog_refusal: 0, keyed_error: 1}


def _key_index(callee, seen: frozenset = frozenset()) -> int | None:
    """Where a maker of a keyed refusal takes its catalog key: a canonical maker, or a
    first-party helper taking a positional-only ``key`` it hands on, as it is, to a maker
    (such as a module's refuse(key, /, **params))."""
    if callee in _MAKERS:
        return _MAKERS[callee]
    if not (inspect.isfunction(callee) and _first_party(callee)) or callee in seen:
        return None
    params = list(inspect.signature(callee).parameters.values())
    index = next((i for i, p in enumerate(params) if p.name == "key" and p.kind is p.POSITIONAL_ONLY), None)
    tree = _tree(callee) if index is not None else None
    if tree is None:
        return None
    scope = _scope(callee, tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (inner := _key_index(_resolve(node.func, scope), seen | {callee})) is not None \
                and len(node.args) > inner and isinstance(node.args[inner], ast.Name) and node.args[inner].id == "key":
            return index
    return None


def _first_party(obj) -> bool:
    return (getattr(obj, "__module__", None) or "").split(".")[0].startswith("celerp")


def _tree(fn):
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    except (OSError, TypeError, SyntaxError, IndexError):
        return None


def _scope(fn, tree) -> dict:
    """The names ``fn`` sees: its module's, its closure's and its own imports."""
    scope = dict(fn.__globals__)
    try:
        scope.update(inspect.getclosurevars(fn).nonlocals)
    except (TypeError, ValueError):
        pass
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            try:
                module = importlib.import_module(node.module)
            except ImportError:
                continue
            for alias in node.names:
                scope[alias.asname or alias.name] = getattr(module, alias.name, None)
    return scope


def _resolve(node, scope):
    if isinstance(node, ast.Name):
        return scope.get(node.id, getattr(builtins, node.id, None))
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, scope)
        if isinstance(base, (types.ModuleType, type)):
            return getattr(base, node.attr, None)
    return None


def _argument(callee, call, name):
    """The expression ``call`` (which builds the class or calls the function ``callee``)
    passes as ``name``: None when it passes none, False when ``callee`` takes no such argument."""
    target = callee.__init__ if isinstance(callee, type) else callee
    params = [p for p in inspect.signature(target).parameters if p != "self"]
    if name not in params:
        return False
    return next((k.value for k in call.keywords if k.arg == name),
                call.args[params.index(name)] if len(call.args) > params.index(name) else None)


def _success(call) -> bool:
    """Whether a Response built by ``call`` answers a success: its status a literal below
    400, or left at the class's own (200, or 204 for an empty Response)."""
    status = next((k.value for k in call.keywords if k.arg == "status_code"),
                  call.args[1] if len(call.args) > 1 else None)
    return status is None or (isinstance(status, ast.Constant) and isinstance(status.value, int) and status.value < 400)


def _given(fn, tree, detail) -> bool:
    """Whether ``detail`` is one of ``fn``'s parameters, passed on as it was given."""
    return isinstance(detail, ast.Name) and detail.id in inspect.signature(fn).parameters and not any(
        isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and any(
            isinstance(t, ast.Name) and t.id == detail.id for t in getattr(n, "targets", [getattr(n, "target", None)]))
        for n in ast.walk(tree))


def _held_keyed(value, every: bool = False) -> bool:
    """Whether a refusal built ahead of time is keyed: a ``refusal`` whose key is in the
    catalog or, with ``every``, a mapping of them."""
    if isinstance(value, dict) and "message_key" in value:
        return value["message_key"] in _EN
    return every and isinstance(value, dict) and bool(value) and all(_held_keyed(v) for v in value.values())


def _slot_handlers() -> list[tuple]:
    """Every handler a module contributes to a slot, as (function, owner): core calls them by
    the dotted path the module registers, never by a name a route's code spells, so a walk
    from the routes alone never reaches the refusals they raise on a route's behalf."""
    from celerp.modules.slots import all_slots, resolve_handler

    found = []
    for slot, entries in all_slots().items():
        # Unattended operations do not answer an agent request. Their helpers
        # remain covered when an agent-callable route actually invokes them.
        if slot == "periodic_operation":
            continue
        for entry in entries:
            if isinstance(entry.get("handler"), str):
                obj = resolve_handler(entry["handler"])
                found += [(f, obj) for f in vars(obj).values() if inspect.isfunction(f)] \
                    if isinstance(obj, type) else [(obj, None)]
    assert found
    return found


def test_periodic_operations_are_not_agent_route_roots():
    """Background-only operations cannot directly return refusals to an AI caller."""
    from celerp.modules.slots import all_slots, resolve_handler

    handlers = {fn for fn, _owner in _slot_handlers()}
    entries = all_slots().get("periodic_operation") or []
    assert entries
    assert all(
        resolve_handler(entry["handler"]) not in handlers
        for entry in entries if isinstance(entry.get("handler"), str)
    )


def _exception_handlers(app) -> list[tuple]:
    """Every exception handler the app answers a refused request with, as (handler, the
    exception class it answers, None for a status code)."""
    found = [(handler, None if isinstance(key, int) else key) for key, handler in app.exception_handlers.items()]
    assert found
    return found


class _Walk:
    """Every function an agent route runs, followed through every first-party name its
    body refers to (calls, callbacks, exception classes and their methods). A route's
    dependencies are not followed: they authenticate the request the AI makes with the
    confirming person's own session before the route runs."""

    def __init__(self, app):
        self.unkeyed: list[str] = []
        self.raised: list[tuple[type, bool, str]] = []  # (exception class, keyed text, where)
        self.carried: dict[type, str] = {}  # exception classes whose text a detail carries
        self._returns: dict = {}
        self._passers: dict = {}
        todo = [(route.endpoint, None) for _path, _methods, route in _api_routes(app)
                if (route.openapi_extra or {}).get("x-celerp-agent") is True]
        assert todo
        todo += _slot_handlers() + [(handler, None) for handler, _ in _exception_handlers(app)]
        walked = self.walked = set()
        while todo:
            fn, owner = todo.pop()
            fn = inspect.unwrap(fn)
            if fn in walked or not inspect.isfunction(fn) or not _first_party(fn):
                continue
            walked.add(fn)
            tree = _tree(fn)
            if tree is not None:
                todo.extend(self._visit(fn, owner, tree))

    def _where(self, fn, node) -> str:
        path = Path(inspect.getsourcefile(fn)).resolve()
        line = inspect.getsourcelines(fn)[1] + node.lineno - 1
        return f"{path.relative_to(_ROOT) if path.is_relative_to(_ROOT) else path}:{line}"

    def _visit(self, fn, owner, tree):
        scope = _scope(fn, tree)
        for node in (n for stmt in tree.body for n in ast.walk(stmt)):
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                obj = _resolve(node, scope)
                if isinstance(obj, type) and _first_party(obj):
                    yield from ((f, obj) for f in vars(obj).values() if inspect.isfunction(f))
                elif callable(obj) and not isinstance(obj, type):
                    yield obj, None
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                cls = _resolve(node.exc.func, scope)
                if isinstance(getattr(cls, "__self__", None), type) and issubclass(cls.__self__, Refused):
                    # Refused.of(detail): the refusal it raises is the detail it is given.
                    if not (node.exc.args and self._keyed(node.exc.args[0], fn, scope, tree)):
                        self.unkeyed.append(f"{self._where(fn, node)} raise {ast.unparse(node.exc)}")
                elif isinstance(cls, type) and issubclass(cls, Refused):
                    key = node.exc.args[0] if node.exc.args else None
                    if isinstance(key, ast.Constant) and key.value not in _EN:
                        self.unkeyed.append(f"{self._where(fn, node)} raise {ast.unparse(node.exc)} (key not in en.json)")
                elif isinstance(cls, type) and issubclass(cls, Exception) and _first_party(cls) \
                        and not issubclass(cls, HTTPException):
                    text = node.exc.args[0] if node.exc.args else None
                    self.raised.append((cls, text is not None and self._keyed(text, fn, scope, tree),
                                        f"{self._where(fn, node)} raise {ast.unparse(node.exc)}"))
            if not isinstance(node, ast.Call):
                continue
            callee = _resolve(node.func, scope)
            if ast.unparse(node.func) == "super().__init__" and owner is not None:
                callee = owner.__mro__[1]
            if (index := _key_index(callee)) is not None:
                key = node.args[index] if len(node.args) > index else None
                if isinstance(key, ast.Constant) and key.value not in _EN:
                    self.unkeyed.append(f"{self._where(fn, node)} {ast.unparse(node)} (key not in en.json)")
                continue
            if inspect.isfunction(callee) and _first_party(callee) and (given := self._passes(callee)):
                # A helper raising the detail it is given: the detail is the one each call passes.
                detail = _argument(callee, node, given)
                if detail is not None and not self._keyed(detail, fn, scope, tree):
                    self.unkeyed.append(f"{self._where(fn, node)} {ast.unparse(node)}")
                continue
            if isinstance(callee, type) and issubclass(callee, Response) and fn is not error_response \
                    and not _success(node):
                # A body built here skips the one builder, which keys a t() text (error_body).
                self.unkeyed.append(f"{self._where(fn, node)} {ast.unparse(node)} (answer through error_response)")
                continue
            if not (isinstance(callee, type) and issubclass(callee, HTTPException)):
                continue
            detail = _argument(callee, node, "detail")
            if detail is False:
                continue  # the class builds its detail in its own __init__, walked with it
            if _given(fn, tree, detail):
                continue  # a subclass or helper passes its detail on: checked where it is built or called
            if detail is None or not self._keyed(detail, fn, scope, tree):
                self.unkeyed.append(f"{self._where(fn, node)} {ast.unparse(detail) if detail else '<no detail>'}")

    def _passes(self, fn) -> str | None:
        """The parameter a helper raises as an HTTPException's detail, as it was given."""
        fn = inspect.unwrap(fn)
        if fn not in self._passers:
            self._passers[fn] = None
            tree = _tree(fn)
            scope = _scope(fn, tree) if tree is not None else {}
            for node in ast.walk(tree or ast.Module(body=[])):
                callee = _resolve(node.func, scope) if isinstance(node, ast.Call) else None
                if isinstance(callee, type) and issubclass(callee, HTTPException):
                    detail = _argument(callee, node, "detail")
                    if _given(fn, tree, detail):
                        self._passers[fn] = detail.id
        return self._passers[fn]

    def _caught(self, use, tree, scope) -> list:
        """The exception classes the name ``use`` refers to is bound to by the
        ``except ... as`` clause it is used in."""
        classes = []
        for handler in ast.walk(tree):
            if isinstance(handler, ast.ExceptHandler) and handler.name == use.id and handler.type is not None \
                    and handler.lineno <= use.lineno <= handler.end_lineno:
                types_ = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                classes.extend(_resolve(n, scope) for n in types_)
        return classes

    def _keyed(self, node, fn, scope, tree) -> bool:
        """Whether a detail (or the text an exception carries) is keyed or translated."""
        keyed = lambda n: self._keyed(n, fn, scope, tree)  # noqa: E731
        if isinstance(node, ast.IfExp):
            return keyed(node.body) and keyed(node.orelse)
        if isinstance(node, ast.BoolOp):
            return all(keyed(v) for v in node.values)
        if isinstance(node, ast.Await):
            return keyed(node.value)  # what a coroutine helper returns
        if isinstance(node, ast.Dict):
            fields = dict((getattr(k, "value", None), v) for k, v in zip(node.keys, node.values) if k is not None)
            if "message_key" in fields or ("message" in fields and keyed(fields["message"])):
                return True
            if any(k is None and keyed(v) for k, v in zip(node.keys, node.values)):
                return True
            return "errors" in fields and isinstance(fields["errors"], ast.Name) \
                and self._list_keyed(fields["errors"].id, fn, scope, tree)
        if isinstance(node, ast.Name):
            values = [n.value for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value
                      and any(isinstance(target, ast.Name) and target.id == node.id
                              for target in getattr(n, "targets", [getattr(n, "target", None)]))]
            if not values and node.id in scope:
                return _held_keyed(scope[node.id])  # a refusal the module builds once
            return bool(values) and all(keyed(v) for v in values)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in scope:
            return _held_keyed(scope[node.value.id], every=True)  # one of a module's refusals
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and isinstance(node.func.value, ast.Name) and node.func.value.id in scope:
            held = scope[node.func.value.id]
            return _held_keyed(held, every=True) and all(keyed(a) for a in node.args[1:])
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr == "detail":
            # A caught exception's detail: keyed when its class keys it.
            classes = self._caught(node.value, tree, scope)
            return bool(classes) and all(self._carries_detail(c, self._where(fn, node)) for c in classes)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call) and node.attr == "detail":
            # The detail of an exception a helper builds: keyed when the class it returns keys it.
            callee = _resolve(node.value.func, scope)
            try:
                cls = typing.get_type_hints(callee).get("return") if inspect.isfunction(callee) else callee
            except (NameError, TypeError):
                cls = None
            return self._carries_detail(cls, self._where(fn, node))
        if isinstance(node, ast.Call):
            callee = _resolve(node.func, scope)
            if callee is str and len(node.args) == 1 and isinstance(node.args[0], ast.Name):
                # A caught exception's text: keyed when every raise of its class is.
                classes = self._caught(node.args[0], tree, scope)
                if not classes or not all(isinstance(c, type) and _first_party(c) for c in classes):
                    return False
                self.carried.update((c, self._where(fn, node)) for c in classes)
                return True
            if callee is i18n.t and node.args:
                key = node.args[0]
                return isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in _EN
            if (index := _key_index(callee)) is not None:
                key = node.args[index] if len(node.args) > index else None
                return not isinstance(key, ast.Constant) or key.value in _EN
            if inspect.isfunction(callee) and _first_party(callee):
                return self._returns_keyed(callee)
        return False

    def _list_keyed(self, name: str, fn, scope, tree) -> bool:
        """A list of refusals built in ``fn``: every entry appended to it is keyed."""
        added = [n.args[0] for n in ast.walk(tree) if isinstance(n, ast.Call) and n.args
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "append"
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == name]
        return bool(added) and all(self._keyed(a, fn, scope, tree) for a in added)

    def _returns_keyed(self, fn) -> bool:
        """A helper that builds a detail: every value it returns (other than None) is keyed."""
        fn = inspect.unwrap(fn)
        if fn not in self._returns:
            self._returns[fn] = False
            tree = _tree(fn)
            scope = _scope(fn, tree) if tree is not None else {}
            returns = [n.value for n in ast.walk(tree or ast.Module(body=[])) if isinstance(n, ast.Return)
                       and n.value is not None and not (isinstance(n.value, ast.Constant) and n.value.value is None)]
            self._returns[fn] = bool(returns) and all(self._keyed(r, fn, scope, tree) for r in returns)
        return self._returns[fn]

    def _carries_detail(self, cls, where: str) -> bool:
        """An exception class whose ``detail`` is keyed: an HTTPException subclass the walk
        checks where it builds it, a class whose __init__ sets ``self.detail`` keyed, or one
        that keeps the detail it is given, whose every raise must then give a keyed one."""
        if not isinstance(cls, type) or not _first_party(cls):
            return False
        if issubclass(cls, HTTPException):
            return True
        for base in cls.__mro__:
            init = vars(base).get("__init__")
            tree = _tree(init) if inspect.isfunction(init) else None
            if tree is None:
                continue
            scope = _scope(init, tree)
            sets = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(
                ast.unparse(target) == "self.detail" for target in n.targets)]
            given = list(inspect.signature(init).parameters)[1:2]
            if sets and all(isinstance(v, ast.Name) and v.id in given for v in sets):
                self.carried[cls] = where
                return True
            if sets:
                return all(self._keyed(v, init, scope, tree) for v in sets)
        return False

    def findings(self) -> list[str]:
        """Every unkeyed detail, and every unkeyed raise of an exception whose text a
        detail carries."""
        carried = [f"{how} (its text is the detail at {where})" for cls, keyed, how in self.raised
                   for carrier, where in self.carried.items() if not keyed and issubclass(cls, carrier)]
        return sorted(set(self.unkeyed + carried))


# The unkeyed refusals agent routes may still raise. Each reaches the AI under the generic
# key error.refusal_as_written (celerp.error_body), so it reads as the English it is, and
# the list only shrinks.
DEBT_FILE = Path(__file__).resolve().parent / "agent_refusals_unkeyed.txt"


def _debt_key(finding: str) -> str:
    """A finding without its line numbers, so an unrelated edit above it does not move it."""
    return re.sub(r"(\.py):\d+", r"\1", finding)


def _debt_problems(findings: list[str], listed: set[str]) -> list[str]:
    """A new unkeyed refusal, and a listed one no longer raised (taken off the list)."""
    found = {_debt_key(f): f for f in findings}
    return [f"unkeyed, not in {DEBT_FILE.name}: {found[key]}" for key in sorted(found.keys() - listed)] + [
        f"in {DEBT_FILE.name}, no longer raised: {key}" for key in sorted(listed - found.keys())]


def test_every_refusal_an_agent_route_can_answer_with_is_keyed_where_it_is_raised():
    """Class guard: a refusal raised by a route the AI calls, or by anything it calls, is
    keyed or translated where it is raised, so the confirmed action that met it reads in
    the reader's language, live and after a reload; or it is one of the listed few."""
    from celerp.main import app

    listed = {line for line in DEBT_FILE.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")}
    problems = _debt_problems(_Walk(app).findings(), listed)
    assert not problems, "\n".join(problems)


def test_translated_catalog_text_keeps_its_key_in_error_responses():
    from celerp.error_body import as_refusal

    detail = as_refusal(i18n.t("subscriptions.err_not_found"))
    assert detail["message_key"] == "subscriptions.err_not_found"
    assert "message" in detail


def test_the_unkeyed_list_only_shrinks():
    """A new unkeyed refusal fails the guard, and so does a listed one that is gone."""
    finding = "celerp/x.py:12 HTTPException(409, 'Taken')"
    assert _debt_problems([finding], set()) == [f"unkeyed, not in {DEBT_FILE.name}: {finding}"]
    assert _debt_problems([finding], {"celerp/x.py HTTPException(409, 'Taken')"}) == []
    assert _debt_problems([], {"celerp/x.py HTTPException(409, 'Taken')"}) == [
        f"in {DEBT_FILE.name}, no longer raised: celerp/x.py HTTPException(409, 'Taken')"]
