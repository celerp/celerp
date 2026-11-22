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

from celerp.accounting_roles import Refused, catalog_refusal, refusal, refusal_detail
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


def _shielded(tree) -> set[int]:
    """The nodes of every ``try`` body a handler catching every exception ends without
    raising: nothing they run can refuse the request, so the walk does not follow them."""
    broad = lambda h: h.type is None or (isinstance(h.type, ast.Name) and h.type.id in ("Exception", "BaseException"))  # noqa: E731
    return {id(n) for node in ast.walk(tree) if isinstance(node, ast.Try)
            and any(broad(h) and not any(isinstance(r, ast.Raise) for r in ast.walk(h)) for h in node.handlers)
            for stmt in node.body for n in ast.walk(stmt)}


def _first_party(obj) -> bool:
    return (getattr(obj, "__module__", None) or "").split(".")[0].startswith("celerp")


def _tree(fn):
    try:
        return ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    except (OSError, TypeError, SyntaxError, IndexError):
        return None


def _own_returns(tree) -> list:
    """The values a function returns itself, not those of a function nested in it."""
    found, todo = [], list(ast.iter_child_nodes(tree))
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return):
            found.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return found


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


def _entries(name: str, scope, tree) -> list | None:
    """The values a local name can hold when every binding of it is an entry of one mapping
    the module holds (``M.get(...)`` or ``M[...]``), else None."""
    bound = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id == name for target in n.targets)]
    held = []
    for value in bound:
        source = value.func.value if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
            and value.func.attr == "get" else value.value if isinstance(value, ast.Subscript) else None
        mapping = _resolve(source, scope) if source is not None else None
        if not isinstance(mapping, dict) or not mapping:
            return None
        held.extend(mapping.values())
    return held or None


def _catalog_key(node, scope, tree=None) -> bool:
    """Whether a t() key is a catalog key wherever it comes from: written there, a constant
    the module holds, either branch of a choice, an entry of a mapping of keys (looked up
    by subscript, or with ``get`` and a fallback that is one too), or a field of an entry of a mapping the module holds, when that
    field of every entry is a catalog key."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id not in scope \
            and tree is not None:
        entries = _entries(node.value.id, scope, tree)
        return entries is not None and all(getattr(e, node.attr, None) in _EN for e in entries)
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in _EN
    if isinstance(node, ast.IfExp):
        return _catalog_key(node.body, scope) and _catalog_key(node.orelse, scope)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
            and isinstance(held := _resolve(node.func.value, scope) if not isinstance(node.func.value, ast.Name)
                           else scope.get(node.func.value.id), dict):
        return bool(held) and all(isinstance(v, str) and v in _EN for v in held.values()) \
            and len(node.args) == 2 and _catalog_key(node.args[1], scope)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
            and isinstance(held := scope.get(node.value.id), dict):
        return bool(held) and all(isinstance(v, str) and v in _EN for v in held.values())
    value = scope.get(node.id) if isinstance(node, ast.Name) else _resolve(node, scope)
    return isinstance(value, str) and value in _EN


def _sets_detail(cls) -> bool:
    """Whether an exception class's __init__ (its own or a base's) sets ``self.detail``."""
    for base in cls.__mro__:
        init = vars(base).get("__init__")
        tree = _tree(init) if inspect.isfunction(init) else None
        if tree is not None and any(isinstance(n, ast.Assign) and any(
                ast.unparse(target) == "self.detail" for target in n.targets) for n in ast.walk(tree)):
            return True
    return False


def _gives_nothing(fn, param: str) -> bool:
    """Whether a parameter left out of a call is empty by default: no refusal to key."""
    default = inspect.signature(fn).parameters[param].default
    return default is None or (isinstance(default, (tuple, list)) and not default)


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
        # Every first-party exception built: (class, whether each argument it is given is
        # keyed, by parameter, "__text__" for its first, where).
        self.raised: list[tuple[type, dict[str, bool], str]] = []
        # Exception classes a detail carries: (where, the parameter it is built from).
        self.carried: dict[type, tuple[str, str]] = {}
        self._built: dict = {}  # HTTPException subclass -> parameters its __init__ builds the detail from
        self._returns: dict = {}
        self._handed_back: dict = {}  # helper -> the parameters it returns as given
        self._passers: dict = {}
        # A parameter of a function that raises with it: (function, parameter) -> where it is
        # used, checked at every call the walk reaches (_called_with).
        self._needs: dict = {}
        self._calls: list = []  # every call of a first-party function: (callee, call, caller, scope, tree)
        self._at_raise = False  # while a detail a raise answers is checked (_detail_keyed)
        todo = [(route.endpoint, None) for _path, _methods, route in _api_routes(app)
                if (route.openapi_extra or {}).get("x-celerp-agent") is True]
        assert todo
        self.handlers = {inspect.unwrap(handler) for handler, _ in _exception_handlers(app)}
        todo += _slot_handlers() + [(handler, None) for handler in self.handlers]
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
        if owner is not None and issubclass(owner, HTTPException) and fn is vars(owner).get("__init__"):
            self._builds(owner)  # the parameters it builds its detail from, checked where it is built
        shielded = _shielded(tree)
        for node in (n for stmt in tree.body for n in ast.walk(stmt) if id(n) not in shielded):
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
                    if not (node.exc.args and self._detail_keyed(node.exc.args[0], fn, scope, tree)):
                        self.unkeyed.append(f"{self._where(fn, node)} raise {ast.unparse(node.exc)}")
                elif isinstance(cls, type) and issubclass(cls, Refused):
                    key = node.exc.args[0] if node.exc.args else None
                    if isinstance(key, ast.Constant) and key.value not in _EN:
                        self.unkeyed.append(f"{self._where(fn, node)} raise {ast.unparse(node.exc)} (key not in en.json)")
            if not isinstance(node, ast.Call):
                continue
            callee = _resolve(node.func, scope)
            if ast.unparse(node.func) == "super().__init__" and owner is not None:
                callee = owner.__mro__[1]
            if inspect.isfunction(callee) and _first_party(callee):
                self._calls.append((inspect.unwrap(callee), node, fn, scope, tree))
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
            if callee is error_response and fn not in self.handlers:
                # An exception handler answers a detail checked where it was raised.
                detail = _argument(callee, node, "detail")
                if detail is None or not self._keyed(detail, fn, scope, tree):
                    self.unkeyed.append(f"{self._where(fn, node)} {ast.unparse(node)}")
                continue
            if isinstance(callee, type) and issubclass(callee, Exception) and _first_party(callee) \
                    and not issubclass(callee, (HTTPException, Refused)):
                # Built here and raised here or later: its text and arguments as given.
                try:
                    given = inspect.signature(callee).bind_partial(
                        *node.args, **{k.arg: k.value for k in node.keywords if k.arg}).arguments
                except (TypeError, ValueError):
                    given = {}
                keyed_args = {name: self._keyed(value, fn, scope, tree) for name, value in given.items()
                              if isinstance(value, ast.expr)}
                keyed_args["__text__"] = bool(node.args) and self._keyed(node.args[0], fn, scope, tree)
                self.raised.append((callee, keyed_args, f"{self._where(fn, node)} {ast.unparse(node)}"))
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
                # The class builds its detail in its own __init__, walked with it, from the
                # parameters checked here, as each call gives them.
                try:
                    given = inspect.signature(callee).bind_partial(
                        *node.args, **{k.arg: k.value for k in node.keywords if k.arg}).arguments
                except TypeError:
                    given = {}
                self.unkeyed.extend(f"{self._where(fn, node)} {ast.unparse(node)}" for name in self._builds(callee)
                                    if not (name in given and self._keyed(given[name], fn, scope, tree)))
                continue
            status = _argument(callee, node, "status_code")
            if isinstance(status, ast.Constant) and status.value == 401:
                continue  # refuses the session, not the request (error_body.unkeyed_refusal)
            if _given(fn, tree, detail):
                continue  # a subclass or helper passes its detail on: checked where it is built or called
            if detail is None or not self._detail_keyed(detail, fn, scope, tree):
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

    def _builds(self, cls) -> list[str]:
        """The parameters an HTTPException subclass's __init__ builds its detail from, as
        given; its own visit reports a detail it builds otherwise unkeyed."""
        init = next((vars(b)["__init__"] for b in cls.__mro__ if inspect.isfunction(vars(b).get("__init__"))), None)
        if init is None or not _first_party(init):
            return []
        if init not in self._built:
            self._built[init] = []
            tree = _tree(init)
            scope = _scope(init, tree) if tree is not None else {}
            self._handed_back.setdefault(init, dict.fromkeys(list(inspect.signature(init).parameters)[1:], False))
            for node in ast.walk(tree or ast.Module(body=[])):
                if isinstance(node, ast.Call) and ast.unparse(node.func) == "super().__init__":
                    detail = _argument(HTTPException, node, "detail")
                    if detail and self._keyed(detail, init, scope, tree):
                        self._built[init] = [n for n, back in self._handed_back[init].items() if back]
        return self._built[init]

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
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return keyed(node.left) and keyed(node.right)  # two lists of refusals joined
        if isinstance(node, ast.Starred):
            return keyed(node.value)  # the entries of a list of refusals, spread into another
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            # Entries of a keyed list kept as they are (a filter), or a keyed entry made for each.
            targets = {g.target.id for g in node.generators if isinstance(g.target, ast.Name)}
            if isinstance(node.elt, ast.Name) and node.elt.id in targets:
                return all(keyed(g.iter) for g in node.generators if ast.unparse(g.target) == node.elt.id)
            return keyed(node.elt)
        if isinstance(node, ast.Dict):
            fields = dict((getattr(k, "value", None), v) for k, v in zip(node.keys, node.values) if k is not None)
            if "message_key" in fields or ("message" in fields and keyed(fields["message"])):
                return True
            if any(k is None and keyed(v) for k, v in zip(node.keys, node.values)):
                return True
            return "errors" in fields and keyed(fields["errors"])
        if isinstance(node, ast.Name):
            values = [n.value for n in ast.walk(tree)
                      if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and n.value
                      and any(isinstance(target, ast.Name) and target.id == node.id
                              for target in getattr(n, "targets", [getattr(n, "target", None)]))]
            if not values and node.id in self._handed_back.get(fn, {}):
                self._handed_back[fn][node.id] = True  # keyed where the caller gives it
                return True
            if not values and node.id in scope:
                return _held_keyed(scope[node.id])  # a refusal the module builds once
            unpacked = self._unpacked(node.id, fn, tree, scope)
            if unpacked is not None:
                return unpacked and all(keyed(v) for v in values)
            if self._at_raise and not values and node.id in inspect.signature(fn).parameters \
                    and fn not in self._handed_back:
                self._needs.setdefault((fn, node.id), self._where(fn, node))  # keyed where each call gives it
                return True
            return bool(values) and all(keyed(v) for v in values) and (
                not any(isinstance(v, ast.List) for v in values) or self._list_keyed(node.id, fn, scope, tree, True))
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in scope:
            return _held_keyed(scope[node.value.id], every=True)  # one of a module's refusals
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            return keyed(node.value)  # an entry of a list of refusals, each keyed
        if isinstance(node, (ast.List, ast.Tuple)):
            return all(keyed(e) for e in node.elts)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and isinstance(node.func.value, ast.Name) and node.func.value.id in scope:
            held = scope[node.func.value.id]
            return _held_keyed(held, every=True) and all(keyed(a) for a in node.args[1:])
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.attr == "detail":
            # A caught exception's detail: keyed when its class keys it.
            classes = self._caught(node.value, tree, scope) or self._annotated(fn, node.value.id)
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
            if callee is str:
                # str() writes a t() text as plain text, dropping its key; a caught
                # exception's text is answered through refusal_detail.
                return False
            if callee is i18n.t and node.args:
                return _catalog_key(node.args[0], scope, tree)
            if callee is refusal_detail and len(node.args) == 1 and isinstance(node.args[0], ast.Name):
                # A caught exception's detail when it has one, else its text: each class
                # it may be keys its detail, or every raise of it is keyed.
                classes = self._caught(node.args[0], tree, scope)
                if not classes or not all(isinstance(c, type) and _first_party(c) for c in classes):
                    return False
                where = self._where(fn, node)
                for cls in classes:
                    if self._carries_detail(cls, where):
                        continue
                    if _sets_detail(cls):
                        return False
                    self.carried[cls] = (where, "__text__")
                return True
            if (index := _key_index(callee)) is not None:
                key = node.args[index] if len(node.args) > index else None
                return not isinstance(key, ast.Constant) or key.value in _EN
            if callee in (list, tuple, sorted) and len(node.args) == 1:
                return keyed(node.args[0])  # the same refusals, as a list
            if isinstance(node.func, ast.Attribute) and node.func.attr == "values" and not node.args \
                    and isinstance(node.func.value, ast.Name):
                return self._dict_values_keyed(node.func.value.id, fn, scope, tree)
            if callee is None and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                # A method of a local: keyed when every class the local is built as keys it.
                classes = self._local_classes(node.func.value.id, tree, scope)
                methods = [getattr(c, node.func.attr, None) for c in classes or ()]
                return bool(methods) and all(
                    inspect.isfunction(m) and _first_party(m) and self._given_keyed(m, node, keyed, bound=True)
                    for m in methods)
            if inspect.isfunction(callee) and _first_party(callee):
                return self._given_keyed(callee, node, keyed)
        return False

    def _detail_keyed(self, detail, fn, scope, tree) -> bool:
        """Whether the detail a raise answers is keyed; a parameter of ``fn`` it is built from
        is then checked at every call of ``fn`` (_called_with), unless the detail is unkeyed anyway."""
        before, self._at_raise = set(self._needs), True
        try:
            keyed = self._keyed(detail, fn, scope, tree)
        finally:
            self._at_raise = False
        if not keyed:
            for need in set(self._needs) - before:
                del self._needs[need]
        return keyed

    def _dict_values_keyed(self, name: str, fn, scope, tree) -> bool:
        """The values of a local dict, each keyed: it is built as a literal, and every entry
        set on it (``name[k] = v``) is keyed."""
        built = [n.value for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value
                 and any(isinstance(t, ast.Name) and t.id == name for t in getattr(n, "targets", None) or [n.target])]
        set_ = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id == name for t in n.targets)]
        return bool(built) and all(isinstance(b, ast.Dict) for b in built) and all(
            self._keyed(v, fn, scope, tree) for v in [*(v for b in built for v in b.values), *set_])

    def _given_keyed(self, callee, call, keyed, bound: bool = False, at: int | None = None) -> bool:
        """Whether ``call`` of the helper ``callee`` (a method when ``bound``) gets a keyed
        value: every value it returns (or the ``at`` entry of each tuple it returns) is keyed,
        and every parameter it hands back is keyed where this call gives it."""
        if not (self._returns_keyed(callee) if at is None else self._returns_keyed_at(callee, at)):
            return False
        handed = [name for name, back in self._handed_back[inspect.unwrap(callee)].items() if back]
        try:
            given = inspect.signature(callee).bind_partial(
                *([call.func.value] if bound else []), *call.args,
                **{k.arg: k.value for k in call.keywords if k.arg}).arguments
        except TypeError:
            return False
        return all(keyed(given[name]) if name in given else _gives_nothing(callee, name) for name in handed)

    def _unpacked(self, name: str, fn, tree, scope) -> bool | None:
        """For a local bound by unpacking what first-party helpers return (``a, b = f()``):
        whether its entry of each is keyed; None when no such binding binds it."""
        found = None
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)):
                continue
            for target in n.targets:
                names = [e.id if isinstance(e, ast.Name) else None for e in getattr(target, "elts", [])]
                if isinstance(target, ast.Tuple) and name in names:
                    callee = _resolve(n.value.func, scope)
                    keyed = lambda v: self._keyed(v, fn, scope, tree)  # noqa: E731
                    found = (found is not False and inspect.isfunction(callee) and _first_party(callee)
                             and self._given_keyed(callee, n.value, keyed, at=names.index(name)))
        return found

    def _returns_keyed_at(self, fn, at: int) -> bool:
        """A helper returning tuples: the ``at`` entry of every tuple it returns is keyed."""
        fn = inspect.unwrap(fn)
        if (fn, at) not in self._returns:
            self._returns[(fn, at)] = False
            self._handed_back.setdefault(fn, dict.fromkeys(inspect.signature(fn).parameters, False))
            tree = _tree(fn)
            scope = _scope(fn, tree) if tree is not None else {}
            returns = [n.value for n in _own_returns(tree or ast.Module(body=[])) if n.value is not None]
            self._returns[(fn, at)] = bool(returns) and all(
                isinstance(r, ast.Tuple) and len(r.elts) > at and self._keyed(r.elts[at], fn, scope, tree)
                for r in returns)
        return self._returns[(fn, at)]

    def _local_classes(self, name: str, tree, scope) -> list | None:
        """The classes a local is built as: each binding a call of a first-party helper whose
        return annotation names them (a class, or one of them or None)."""
        values = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
        classes = []
        for value in values:
            callee = _resolve(value.func, scope) if isinstance(value, ast.Call) else None
            if not (inspect.isfunction(callee) and _first_party(callee)):
                return None
            try:
                hint = typing.get_type_hints(callee).get("return")
            except (NameError, TypeError):
                return None
            found = [c for c in (typing.get_args(hint) or (hint,)) if c is not type(None)]
            if not found or not all(isinstance(c, type) and _first_party(c) for c in found):
                return None
            classes += found
        return classes or None

    def _list_keyed(self, name: str, fn, scope, tree, written: bool = False) -> bool:
        """A list of refusals built in ``fn``: every entry appended to it is keyed (and, with
        ``written``, any entry its literal holds, checked by the caller, is enough)."""
        added = [n.args[0] for n in ast.walk(tree) if isinstance(n, ast.Call) and n.args
                 and isinstance(n.func, ast.Attribute) and n.func.attr in ("append", "extend")
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == name]
        if not all(self._keyed(a, fn, scope, tree) for a in added):
            return False
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            # A helper given the list appends its own refusals to it.
            helper = _resolve(call.func, scope)
            if not (inspect.isfunction(helper) and _first_party(helper)):
                continue
            try:
                bound = inspect.signature(helper).bind_partial(
                    *call.args, **{k.arg: k.value for k in call.keywords if k.arg}).arguments
            except TypeError:
                continue
            for param, value in bound.items():
                if isinstance(value, ast.Name) and value.id == name and (inner := _tree(helper)) is not None:
                    if not self._list_keyed(param, helper, _scope(helper, inner), inner, True):
                        return False
                    added.append(value)
        return written or bool(added)

    @staticmethod
    def _annotated(fn, name: str) -> list:
        """The exception class a parameter of ``fn`` is annotated with."""
        try:
            hint = typing.get_type_hints(fn).get(name)
        except (NameError, TypeError):
            return []
        return [hint] if isinstance(hint, type) and issubclass(hint, Exception) else []

    def _returns_keyed(self, fn) -> bool:
        """A helper that builds a detail: every value it returns (other than None) is keyed,
        or is a parameter it hands back as given, keyed where each caller gives it."""
        fn = inspect.unwrap(fn)
        if fn not in self._returns:
            self._returns[fn] = False
            self._handed_back[fn] = dict.fromkeys(inspect.signature(fn).parameters, False)
            tree = _tree(fn)
            scope = _scope(fn, tree) if tree is not None else {}
            returns = [n.value for n in _own_returns(tree or ast.Module(body=[]))
                       if n.value is not None and not (isinstance(n.value, ast.Constant) and n.value.value is None)]
            self._returns[fn] = bool(returns) and all(self._keyed(r, fn, scope, tree) for r in returns)
        return self._returns[fn]

    def _carries_detail(self, cls, where: str) -> bool:
        """An exception class whose ``detail`` is keyed: an HTTPException subclass the walk
        checks where it builds it, a class whose __init__ sets ``self.detail`` keyed, or one
        that keeps the detail it is given, whose every raise must then give a keyed one."""
        if isinstance(cls, type) and issubclass(cls, HTTPException):
            return True  # every HTTPException the walk reaches is checked where it is built
        if not isinstance(cls, type) or not _first_party(cls):
            return False
        for base in cls.__mro__:
            init = vars(base).get("__init__")
            tree = _tree(init) if inspect.isfunction(init) else None
            if tree is None:
                continue
            scope = _scope(init, tree)
            sets = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign) and any(
                ast.unparse(target) == "self.detail" for target in n.targets)]
            given = list(inspect.signature(init).parameters)[1:]
            if sets and len({ast.unparse(v) for v in sets}) == 1 and isinstance(sets[0], ast.Name) \
                    and sets[0].id in given and _given(init, tree, sets[0]):
                self.carried[cls] = (where, sets[0].id)  # each build of it gives a keyed one
                return True
            if sets:
                return all(self._keyed(v, init, scope, tree) for v in sets)
        return False

    def findings(self) -> list[str]:
        """Every unkeyed detail, and every unkeyed raise of an exception whose text a
        detail carries."""
        carried = [f"{how} (its text is the detail at {where})" for cls, keyed, how in self.raised
                   for carrier, (where, name) in self.carried.items()
                   if issubclass(cls, carrier) and keyed.get(name) is False]
        return sorted(set(self.unkeyed + carried + self._called_with()))

    def _called_with(self) -> list[str]:
        """Every call that gives a function raising with its parameter an unkeyed value, and
        every such parameter no walked call gives (a route's own: what the request sent)."""
        found, checked = [], set()
        while len(checked) < len(self._needs):
            for (fn, param), where in list(self._needs.items()):
                if (fn, param) in checked:
                    continue
                checked.add((fn, param))
                calls = [c for c in self._calls if c[0] is fn]
                if not calls:
                    found.append(f"{where} {param} (no call the walk reaches gives it)")
                for _callee, call, caller, scope, tree in calls:
                    try:
                        given = inspect.signature(fn).bind_partial(
                            *call.args, **{k.arg: k.value for k in call.keywords if k.arg}).arguments
                    except TypeError:
                        given = {}
                    ok = (isinstance(given[param], ast.expr) and self._keyed(given[param], caller, scope, tree)
                          if param in given else _gives_nothing(fn, param))
                    if not ok:
                        found.append(f"{self._where(caller, call)} {ast.unparse(call)} ({param}, raised at {where})")
        return found


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


def test_a_dict_refusal_whose_message_is_catalog_text_keeps_its_key_and_its_fields():
    from celerp.error_body import as_refusal, keyed

    detail = as_refusal({"code": "import_items_modified", "message": i18n.t("inventory.err_import_items_changed"),
                         "entity_ids": ["item:1"]})
    assert keyed(detail) and detail["message_key"] == "inventory.err_import_items_changed"
    assert detail["code"] == "import_items_modified" and detail["entity_ids"] == ["item:1"]


def test_a_try_whose_broad_handler_never_raises_is_not_followed():
    tree = ast.parse(textwrap.dedent("""
        def f():
            try:
                shielded()
            except Exception:
                return None
            try:
                followed()
            except Exception:
                raise
            try:
                narrow()
            except ValueError:
                return None
    """)).body[0]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in _shielded(tree)}
    assert "shielded" not in names and {"followed", "narrow"} <= names


def test_the_unkeyed_list_only_shrinks():
    """A new unkeyed refusal fails the guard, and so does a listed one that is gone."""
    finding = "celerp/x.py:12 HTTPException(409, 'Taken')"
    assert _debt_problems([finding], set()) == [f"unkeyed, not in {DEBT_FILE.name}: {finding}"]
    assert _debt_problems([finding], {"celerp/x.py HTTPException(409, 'Taken')"}) == []
    assert _debt_problems([], {"celerp/x.py HTTPException(409, 'Taken')"}) == [
        f"in {DEBT_FILE.name}, no longer raised: celerp/x.py HTTPException(409, 'Taken')"]
