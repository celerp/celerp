# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""What the bundled source can reach, read from its syntax alone.

A call graph over celerp/, default_modules/ and ui/: each function, the places it writes
or takes a company's lock itself, and the calls it makes that resolve by name to another
function of the source (a function in the same scope or module, one imported by name, a
function of an imported module, a method of the same class). `doing` follows those
calls to their end, so a guard sees a lock or a write however many wrappers stand between.

A call on any other object (a method of an argument, a callable passed in) does not
resolve: the graph reports what the source names, never a guess.

A write is read from what a call executes and what its receiver holds, not from how the
call is spelled: a statement is followed through method chains and the names it is bound
to, a receiver through annotations and the values its names are bound to. The rule denies
by default: a call given a write statement is a write whatever its name or receiver; every
method called or attribute referenced on a session, connection, engine or session factory
is a write, or is named with its reason in the read lists below; a session call the graph
cannot read counts as an unclassified write, and `unclassified` lists it, with every
reference to a session's method it cannot place on a line.

Standard library only, so the census job reads it without the app installed."""

from __future__ import annotations

import ast
import os
import re
from collections import ChainMap, defaultdict
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = ("celerp", "default_modules", "ui")
_SKIP_PARTS = ("/tests/", "/migrations/", "/__pycache__/")
# The primitives that take a company's lock (celerp/services/company_lock.py).
TAKES_THE_LOCK = frozenset({"lock_company", "locked_company", "lock_projections", "hold_company",
                            "lock_company_for_deletion"})
MUTATING = frozenset({"post", "put", "patch", "delete"})
_CLIENTS = frozenset({"c", "client", "_c", "http"})  # names the UI gives an HTTP client into the API
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

# The calls a session or connection runs a statement through: what they are given says what they do.
EXECUTES = frozenset({"execute", "exec_driver_sql", "scalar", "scalars", "stream", "stream_scalars"})
# The calls a session writes a mapped row through.
ROW_WRITES = frozenset({"add", "add_all", "delete", "merge"})
SESSION_CALLS = EXECUTES | ROW_WRITES
# Every other method or attribute of a session or connection, engine or session factory the
# source may use, with why it is not a write site of its own. Any name not listed fails the guard.
DATABASE_READS = {
    "database session": {
        "get": "loads one row by its key",
        "refresh": "reloads a row's attributes from the database",
        "flush": "sends pending row changes; an added, deleted or merged row has its own write site, but a "
                 "change made by setting a loaded row's attribute has none, so it is covered at route level "
                 "(a route that writes needs a test that makes it write), not per site",
        "commit": "ends the transaction and sends pending row changes; it is counted as a commit, not a write "
                  "site, so a change made by setting a loaded row's attribute is covered at route level, not per site",
        "rollback": "discards the transaction",
        "begin": "opens a transaction that commits when its block ends; counted as a commit, like commit",
        "begin_nested": "opens a savepoint",
        "in_transaction": "reports transaction state", "in_nested_transaction": "reports transaction state",
        "close": "returns the connection to its pool",
        "expunge": "detaches a row from the session", "expunge_all": "detaches every row from the session",
        "expire": "marks a row's attributes stale", "expire_all": "marks every row's attributes stale",
        "connection": "the session's connection, itself checked as a session",
        "get_bind": "the engine the session runs on, itself checked as an engine",
        "bind": "the engine the session runs on, itself checked as an engine",
        "sync_session": "the session's synchronous form, itself checked as a session",
        "execution_options": "sets options for the statements it runs later",
        "info": "a dictionary the application keeps on the session",
        "get_transaction": "the open transaction, to tell it apart", "get_nested_transaction": "the open savepoint",
        "dirty": "the rows changed since the last flush", "dialect": "the database dialect",
        "run_sync": "hands the function it is given a synchronous connection; a call that function makes on a "
                    "receiver the scan knows as a session is classified where it is written, and one it cannot "
                    "place (an unannotated parameter) is covered at route level, not per site",
    },
    "database engine": {
        "connect": "opens a connection, itself checked as a session",
        "begin": "opens a connection in a transaction, itself checked as a session",
        "dispose": "closes the engine's pooled connections",
        "sync_engine": "the engine's synchronous form, itself checked as an engine",
        "url": "the database address", "dialect": "the database dialect", "pool": "the connection pool, for its counts",
    },
    "database session factory": {
        "begin": "opens a session in a transaction that commits when its block ends, itself checked as a "
                 "session; counted as a commit, like a session's commit",
    },
}
# What an expression can hold, as Graph.kind names it.
_DATABASE = "database "
SESSION = "database session"  # an AsyncSession, Session or connection: it executes and adds
FACTORY = "database session factory"  # calling it opens a session
ENGINE = "database engine"  # connect() and begin() open a connection
SQLA = "sqlalchemy"  # a name of the sqlalchemy package, as "sqlalchemy <name>"
_SQL_WRITES = {f"statement {op}" for op in ("insert", "update", "delete", "merge", "truncate", "multiple")}
_SQL_READS = {"statement select": "a SELECT",
              "statement control": "a session control statement (SET, LOCK, SAVEPOINT and the like)",
              "statement ddl": "schema DDL, which the observer counts as no data write"}
# What a value annotated with a sqlalchemy type holds.
_SQLA_TYPES = {"AsyncSession": SESSION, "Session": SESSION, "AsyncConnection": SESSION, "Connection": SESSION,
               "async_sessionmaker": FACTORY, "sessionmaker": FACTORY, "AsyncEngine": ENGINE, "Engine": ENGINE,
               "Select": "statement select", "Insert": "statement insert", "Update": "statement update",
               "Delete": "statement delete"}
_SQLA_MADE = {"AsyncSession": SESSION, "Session": SESSION, "async_sessionmaker": FACTORY, "sessionmaker": FACTORY,
              "create_async_engine": ENGINE, "create_engine": ENGINE,
              "insert": "statement insert", "update": "statement update", "delete": "statement delete",
              "select": "statement select", "union": "statement select", "union_all": "statement select",
              "exists": "statement select", "values": "statement select",
              "object_session": SESSION, "async_object_session": SESSION}
_SQL_FIRST = {"INSERT": "insert", "UPDATE": "update", "DELETE": "delete", "MERGE": "merge", "TRUNCATE": "truncate",
              "SELECT": "select", "VALUES": "select", "SHOW": "select", "EXPLAIN": "select", "TABLE": "select",
              **dict.fromkeys(("SET", "RESET", "LOCK", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE",
                               "DISCARD", "LISTEN", "NOTIFY", "UNLISTEN", "ANALYZE", "VACUUM"), "control"),
              **dict.fromkeys(("CREATE", "ALTER", "DROP", "COMMENT", "GRANT", "REVOKE", "REINDEX"), "ddl")}
_BUILTIN = {"set": "set", "frozenset": "set", "dict": "dict:", "list": "list:", "tuple": "list:", "sorted": "list:",
            "str": "value", "int": "value", "float": "value", "bool": "value", "bytes": "value", "len": "value"}
_ANNOTATED = {**dict.fromkeys(("set", "Set", "frozenset", "FrozenSet", "MutableSet", "AbstractSet"), "set"),
              **dict.fromkeys(("dict", "Dict", "defaultdict", "DefaultDict", "Mapping", "MutableMapping",
                               "OrderedDict", "Counter"), "dict:"),
              **dict.fromkeys(("list", "List", "Sequence", "tuple", "Tuple", "Iterable", "Iterator", "deque"), "list:"),
              **dict.fromkeys(("str", "int", "float", "bool", "bytes"), "value")}
_YIELDS = {"AsyncIterator", "AsyncGenerator", "Iterator", "Generator", "AsyncContextManager", "ContextManager",
           "AbstractAsyncContextManager", "AbstractContextManager", "Awaitable", "Optional", "Annotated"}


def _sql_kind(text: str | None) -> str | None:
    """A statement kind from the SQL a literal starts with; a data-changing CTE is a write,
    and so is text that holds more than one statement."""
    if text is None:
        return None
    body = re.sub(r"--[^\n]*|/\*.*?\*/", " ", text, flags=re.S).lstrip(" \t\r\n(")
    first = body.split(None, 1)[0].upper() if body.split() else ""
    if first not in _SQL_FIRST and first != "WITH":
        return None
    if len([part for part in re.sub(r"'(?:[^']|'')*'", "''", body).split(";") if part.strip()]) > 1:
        return "statement multiple"
    if first == "WITH":
        op = re.search(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", body, re.I)
        return f"statement {op.group(1).lower()}" if op else "statement select"
    return f"statement {_SQL_FIRST[first]}"


_CYCLE = "cycle"  # a value still being worked out, met again while working it out


def _combine(kinds) -> str | None:
    """What a name holds across all the values bound to it: a session when any is one,
    else the one kind they share; None when they differ or one is unknown."""
    kinds = {k for k in kinds if k not in ("none", _CYCLE)}
    if SESSION in kinds:
        return SESSION
    if kinds and all(k is not None and k.startswith("statement ") for k in kinds):
        writes = sorted(kinds & _SQL_WRITES)
        return writes[0] if writes else "statement select" if "statement select" in kinds else min(kinds)
    return kinds.pop() if len(kinds) == 1 else None


@dataclass
class Function:
    module: str
    qualname: str
    path: str  # relative to the repository
    first: int  # the line its source starts on: its first decorator, else its def
    node: ast.AST = field(repr=False)
    effects: list[tuple[int, str]] = field(default_factory=list)  # (line, what) it does itself
    calls: list[tuple[int, str]] = field(default_factory=list)  # (line, the key of a function it calls)
    scope: ChainMap = field(default_factory=ChainMap, repr=False)  # the names visible in it
    cls: str | None = None  # the class it is a method of
    parent: "Function | None" = field(default=None, repr=False)  # the function it is defined in

    @property
    def key(self) -> str:
        return f"{self.module}.{self.qualname}"


def _called(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else ""


def _last(node) -> str:
    """The last name of a dotted expression (a call's callee for a call)."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Subscript):
        node = node.value
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""



def _own(root):
    """The nodes under *root* that run in its own frame: not those of a function, class or
    lambda defined inside it."""
    stack = list(ast.iter_child_nodes(root))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (*_FUNCTIONS, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _receiver(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        value = func.value
        if isinstance(value, ast.Attribute):
            return value.attr
        if isinstance(value, ast.Name):
            return value.id
    return ""


def _written(kind: str | None) -> str | None:
    """The write a statement kind makes, as an effect; None for anything else."""
    return f"write {kind.rpartition(' ')[2]}" if kind in _SQL_WRITES else None


def _statement(name: str, statement: str | None) -> tuple[str | None, str | None]:
    """What running *statement* through call *name* does."""
    if statement in _SQL_READS:
        return None, f"executes {_SQL_READS[statement]}"
    return _written(statement) or f"write unclassified {name}", None


def commits(node, kind=lambda expr: None) -> bool:
    """Whether *node* is a commit: commit on a session, or begin on a session or session
    factory (the block it opens commits when it ends), referenced in any form: called,
    aliased (done = session.commit) or looked up by getattr with a constant name."""
    if isinstance(node, ast.Attribute):
        held, name = node.value, node.attr
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" \
            and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        held, name = node.args[0], node.args[1].value
    else:
        return False
    return name in ("commit", "begin") and kind(held) in ((SESSION,) if name == "commit" else (SESSION, FACTORY))


def classify(node: ast.Call, kind=lambda expr: None, decorator: bool = False,
             built=None) -> tuple[str | None, str | None]:
    """What a call does itself, and for a call that writes nothing the named rule it reads
    by. A call takes a company's lock, writes through a session, commits, emits an event,
    or sends a mutating request to the API. *kind* tells what an expression holds
    (Graph.kind): a session, a statement, or a value that is no session; *built* tells the
    same with SQL in a string literal read as text, which only an executing call runs."""
    built = built or kind
    name, receiver = _called(node), _receiver(node)
    if name in TAKES_THE_LOCK:
        return f"lock {name}", None
    if name == "emit_event":
        return "write emit_event", None
    if isinstance(node.func, ast.Attribute) and (name == "commit" or commits(node.func, kind)) or commits(node, kind):
        return "commit", None
    if name in MUTATING and receiver in _CLIENTS:
        return f"api {name.upper()}", None
    if name == "request" and receiver in _CLIENTS and node.args and isinstance(node.args[0], ast.Constant) \
            and str(node.args[0].value).lower() in MUTATING:
        return f"api {str(node.args[0].value).upper()}", None
    if decorator:
        return None, f"{name} as a decorator, which registers a function" if name in SESSION_CALLS else None
    method = isinstance(node.func, ast.Attribute)
    held = kind(node.func.value) if method else None
    if held in DATABASE_READS:
        # A session, engine or factory: each method is a write, or named with its reason.
        if held == SESSION and name in EXECUTES:
            return _statement(name, kind(node.args[0]) if node.args else None)
        if held == SESSION and name in ROW_WRITES:
            return f"write session.{name}", None
        if name in DATABASE_READS[held]:
            return None, f"{name} on a {held}: {DATABASE_READS[held][name]}"
        return f"write unclassified {name}", None
    # Given a write statement, a call writes whatever it is named and whatever receives it,
    # unless it builds a further statement or is a function of the source or a builtin,
    # whose own calls are classified where they are written.
    given = next((w for a in (*node.args, *(k.value for k in node.keywords)) if (w := _written(built(a)))), None)
    if given and not (kind(node) or "").startswith("statement ") \
            and not (kind(node.func) or "").startswith(("function ", "builtin ", "type ")):
        return given, None
    if name not in SESSION_CALLS or not method:
        return None, None
    if held not in (None, _CYCLE):
        return None, f"{name} on {held}"
    if name in EXECUTES:
        if not node.args and name not in ("execute", "exec_driver_sql"):
            return None, f"{name} of a result, which reads what the result holds"
        return _statement(name, kind(node.args[0]) if node.args else None)
    if node.args and name != "add_all":
        # A session adds, deletes and merges mapped rows only: what is passed says what the call is.
        given = kind(node.args[0])
        if given and given.startswith("row "):
            return f"write session.{name}", None
        if given not in (None, _CYCLE) and not given.startswith(_DATABASE):
            return None, f"{name} of {given}, which is no mapped row"
    return f"write unclassified {name}", None


def effect(node: ast.Call, kind=lambda expr: None, decorator: bool = False, built=None) -> str | None:
    return classify(node, kind, decorator, built)[0]


def _module_name(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[0] == "default_modules":
        parts = parts[2:]  # default_modules/<module>/<package>/...
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or rel


def _python_files():
    for top in SOURCES:
        for folder, _, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                path = os.path.join(folder, name)
                if name.endswith(".py") and not any(p in path + "/" for p in _SKIP_PARTS):
                    yield os.path.relpath(path, ROOT)


class Graph:
    def __init__(self, files=None) -> None:
        self.functions: dict[str, Function] = {}
        self.by_source: dict[tuple[str, int], Function] = {}  # (path, first line) -> function
        self._modules: dict[str, dict] = {}  # module -> {"names": {name: key or module}, "path": rel}
        self.classes: dict[str, dict] = {}  # key -> {"node", "module", "scope"}
        self.trees: dict[str, ast.Module] = {}
        self._kinds: dict[tuple, str | None] = {}
        self._open: set[tuple] = set()
        self._hit: set[tuple] = set()
        self._partial: dict[tuple, str | None] = {}
        self._literal_sql = True  # whether SQL in a string literal is read as a statement
        self._bound_names: dict[int, dict] = {}
        self._by_class: dict[str, list] | None = None
        trees = {}
        for rel in files if files is not None else _python_files():
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                    trees[rel] = ast.parse(fh.read())
            except (SyntaxError, UnicodeDecodeError):
                continue
        self.trees = trees
        for rel, tree in trees.items():
            self._index(rel, tree)
        self._roots = {module.split(".")[0] for module in self._modules}
        self._decorators = {id(d) for tree in trees.values() for node in ast.walk(tree)
                            if isinstance(node, (*_FUNCTIONS, ast.ClassDef)) for d in node.decorator_list}
        for function in self.functions.values():
            self._link(function)

    # -- indexing --------------------------------------------------------------

    def _index(self, rel: str, tree: ast.Module) -> None:
        module = _module_name(rel)
        package = module if rel.endswith("__init__.py") else module.rpartition(".")[0]
        info = self._modules.setdefault(module, {"path": rel, "names": {}, "package": package})
        info["tree"] = tree

        def walk(body, prefix: str, scope: ChainMap, cls: str | None, parent: Function | None = None) -> None:
            for node in body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    self._imports(node, scope, package)
                elif isinstance(node, _FUNCTIONS):
                    qual = f"{prefix}{node.name}"
                    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    # Names resolve when the call runs: its own nested definitions, then
                    # every name its enclosing scopes and its module end up holding.
                    fn = Function(module, qual, rel, first, node, scope=scope.new_child(), cls=cls, parent=parent)
                    self.functions[fn.key] = fn
                    self.by_source[(rel, first)] = fn
                    scope[node.name] = fn.key
                    walk(node.body, f"{qual}.", fn.scope, None, fn)
                elif isinstance(node, ast.ClassDef):
                    key = f"{module}.{prefix}{node.name}"
                    self.classes[key] = {"node": node, "module": module, "scope": scope, "parent": parent}
                    scope[node.name] = ("class", key)
                    walk(node.body, f"{prefix}{node.name}.", scope.new_child(), f"{prefix}{node.name}", parent)
                elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
                    blocks = [node.body, getattr(node, "orelse", []), getattr(node, "finalbody", [])]
                    blocks += [h.body for h in getattr(node, "handlers", [])]
                    for block in blocks:
                        walk(block, prefix, scope, cls, parent)
        walk(tree.body, "", ChainMap(info["names"]), None)

    def _imports(self, node, scope: dict, package: str) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    scope[alias.asname] = ("module", alias.name)
                else:
                    scope[alias.name.split(".")[0]] = ("module", alias.name.split(".")[0])
            return
        base = node.module or ""
        if node.level:
            parts = package.split(".")
            parts = parts[:len(parts) - node.level + 1] if node.level > 1 else parts
            base = ".".join(p for p in (*parts, base) if p)
        for alias in node.names:
            scope[alias.asname or alias.name] = ("from", base, alias.name)

    # -- resolution ------------------------------------------------------------

    def _resolve_name(self, target, depth: int = 0):
        """A scope entry to a function key, a module name, or None."""
        if isinstance(target, str) or target is None or depth > 8:
            return target if isinstance(target, str) else None
        if target[0] == "class":
            return target
        if target[0] == "module":
            return ("module", target[1])
        _, base, name = target
        if f"{base}.{name}" in self._modules:
            return ("module", f"{base}.{name}")
        module = self._modules.get(base)
        if module is None:
            return None
        return self._resolve_name(module["names"].get(name), depth + 1)

    def _attribute(self, module: str, name: str):
        info = self._modules.get(module)
        if info is None:
            return None
        if f"{module}.{name}" in self._modules:
            return ("module", f"{module}.{name}")
        return self._resolve_name(info["names"].get(name))

    def _link(self, fn: Function) -> None:
        own = [n for n in ast.walk(fn.node) if isinstance(n, _FUNCTIONS) and n is not fn.node]
        nested = {id(c) for n in own for c in ast.walk(n)}
        local = dict(fn.scope)
        for node in ast.walk(fn.node):  # a function's own imports are visible in all of it
            if isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in nested:
                self._imports(node, local, self._modules[fn.module]["package"])
        called = {id(n.func) for n in ast.walk(fn.node) if isinstance(n, ast.Call)}
        for node in ast.walk(fn.node):
            if id(node) in nested:
                continue
            if isinstance(node, ast.Attribute) and id(node) not in called \
                    and commits(node, lambda expr: self.kind(expr, fn)):
                fn.effects.append((node.lineno, "commit"))  # done = session.commit: a commit wherever it is called
            if not isinstance(node, ast.Call):
                continue
            if what := self.effect(node, fn):
                fn.effects.append((node.lineno, what))
            key = self._callee(node, local, fn)
            if key is not None:
                fn.calls.append((node.lineno, key))

    def _callee(self, node: ast.Call, scope: dict, fn: Function) -> str | None:
        func = node.func
        if isinstance(func, ast.Name):
            found = self._resolve_name(scope.get(func.id))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id in ("self", "cls") and fn.cls:
                found = f"{fn.module}.{fn.cls}.{func.attr}"
            else:
                owner = self._resolve_name(scope.get(func.value.id))
                found = self._attribute(owner[1], func.attr) if isinstance(owner, tuple) and owner[0] == "module" \
                    else None
        else:
            found = None
        return found if isinstance(found, str) and found in self.functions else None

    # -- what a call does --------------------------------------------------------

    def effect(self, node: ast.Call, fn: Function | None, module: str | None = None) -> str | None:
        return effect(node, lambda expr: self.kind(expr, fn, module), id(node) in self._decorators,
                      lambda expr: self.built(expr, fn, module))

    def _walk(self):
        """Every node of the source as (path, node, the innermost function it runs in, or
        None at module level, its module). A function's decorators run in its own frame."""
        for rel, tree in self.trees.items():
            module = _module_name(rel)
            stack = [(child, None) for child in ast.iter_child_nodes(tree)]
            while stack:
                node, fn = stack.pop()
                if isinstance(node, _FUNCTIONS):
                    first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    fn = self.by_source.get((rel, first), fn)
                yield rel, node, fn, module
                stack.extend((child, fn) for child in ast.iter_child_nodes(node))

    def calls(self):
        """Every call of the source, as _walk gives it."""
        for rel, node, fn, module in self._walk():
            if isinstance(node, ast.Call):
                yield rel, node, fn, module

    def unclassified(self) -> list[str]:
        """Each call the graph classifies neither as a write it can read nor by a named read
        rule, and each method or attribute of a session, connection, engine or factory that
        no read list names and is referenced without being called (put = session.add) or
        looked up by getattr, as "path:line name": a write the census cannot place on a line."""
        found, called = [], set()
        for rel, node, fn, module in self._walk():
            if isinstance(node, ast.Call):
                called.add(id(node.func))
                if (self.effect(node, fn, module) or "").startswith("write unclassified "):
                    found.append(f"{rel}:{node.lineno} {_called(node)}")
                if isinstance(node.func, ast.Name) and node.func.id == "getattr" and node.args \
                        and (held := self.kind(node.args[0], fn, module)) in DATABASE_READS:
                    name = node.args[1] if len(node.args) > 1 else None
                    if not (isinstance(name, ast.Constant) and name.value in DATABASE_READS[held]):
                        found.append(f"{rel}:{node.lineno} getattr")
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and id(node) not in called \
                    and (held := self.kind(node.value, fn, module)) in DATABASE_READS \
                    and node.attr not in DATABASE_READS[held]:
                found.append(f"{rel}:{node.lineno} {node.attr}")
        return sorted(found, key=lambda s: (s.rpartition(":")[0], int(s.rpartition(":")[2].split()[0])))

    # -- what an expression holds ------------------------------------------------
    # A kind is a string: SESSION, FACTORY or ENGINE; "statement <op>"; "set", "value",
    # "list:<what its items hold>", "dict:<what its values hold>"; "row <key>" for an
    # instance of a mapped class of the source, "class <key>" for one of any other class and
    # "type <key>" for the class itself, "function <key>", "module <name>";
    # "sqlalchemy <name>" and "outside <dotted name>" for what other packages provide; None
    # when the source does not say.

    def _memo(self, key, compute):
        """*compute*'s answer, kept once it depends on no value still being worked out: a
        value bound in terms of itself (query = query.where(..)) adds nothing to its kind.
        An answer that does depend on one is kept until the outermost question is answered,
        so names bound in terms of each other are worked out once per question, not once
        per path between them."""
        key = (key, self._literal_sql)
        if key in self._kinds:
            return self._kinds[key]
        if key in self._open:
            self._hit.add(key)
            return _CYCLE
        if key in self._partial:
            return self._partial[key]
        if len(self._open) > 80:
            return None
        self._open.add(key)
        try:
            found = compute()
        finally:
            self._open.discard(key)
            self._hit.discard(key)
        if not self._hit:
            self._kinds[key] = found
        else:
            self._partial[key] = found
        if not self._open:
            self._partial.clear()
        return found

    def kind(self, expr, fn: Function | None, module: str | None = None) -> str | None:
        """What *expr* holds when it runs in *fn* (or at the top level of *module*)."""
        module = fn.module if fn is not None else module
        return self._memo((id(expr), id(fn) if fn is not None else module), lambda: self._kind(expr, fn, module))

    def built(self, expr, fn: Function | None, module: str | None = None) -> str | None:
        """What *expr* holds with SQL in a string literal read as plain text: a string is
        SQL only to a call that executes it or to text()."""
        return self._reading(False, lambda: self.kind(expr, fn, module))

    def _reading(self, literal_sql: bool, compute):
        saved, self._literal_sql = self._literal_sql, literal_sql
        try:
            return compute()
        finally:
            self._literal_sql = saved

    def _kind(self, expr, fn, module):
        kind = lambda e: self.kind(e, fn, module)
        if isinstance(expr, ast.Name):
            return self._name(expr.id, fn, module)
        if isinstance(expr, ast.Constant):
            if expr.value is None:
                return "none"
            return (_sql_kind(expr.value) if isinstance(expr.value, str) and self._literal_sql else None) or "value"
        if isinstance(expr, ast.JoinedStr):
            return (_sql_kind("".join(v.value if isinstance(v, ast.Constant) else "x" for v in expr.values))
                    if self._literal_sql else None) or "value"
        if isinstance(expr, ast.Call):
            return self._call(expr, fn, module)
        if isinstance(expr, ast.Attribute):
            return self._attr(kind(expr.value), expr.attr, fn, module)
        if isinstance(expr, ast.Subscript):
            held = kind(expr.value)
            return (held[5:] or None) if held and held.startswith(("dict:", "list:")) and \
                not isinstance(expr.slice, ast.Slice) else held if held == _CYCLE or (held or "").startswith("list:") \
                else None
        if isinstance(expr, (ast.Await, ast.NamedExpr)):
            return kind(expr.value)
        if isinstance(expr, ast.BinOp):
            return kind(expr.left)
        if isinstance(expr, ast.IfExp):
            return _combine([kind(expr.body), kind(expr.orelse)])
        if isinstance(expr, ast.BoolOp):
            return _combine(kind(v) for v in expr.values)
        if isinstance(expr, (ast.Set, ast.SetComp)):
            return "set"
        if isinstance(expr, ast.Dict):
            return "dict:" + (_combine(kind(v) for v in expr.values) or "")
        if isinstance(expr, ast.DictComp):
            return "dict:" + (kind(expr.value) or "")
        if isinstance(expr, (ast.List, ast.Tuple)):
            return "list:" + (_combine(kind(e) for e in expr.elts) or "")
        if isinstance(expr, (ast.ListComp, ast.GeneratorExp)):
            return "list:" + (kind(expr.elt) or "")
        if isinstance(expr, (ast.Compare, ast.UnaryOp)):
            return "value"
        return None

    def _name(self, name: str, fn, module):
        scope = fn
        while scope is not None:
            bound = self._bindings(scope)
            if name in bound:
                return self._held(bound[name], scope, scope.module)
            scope = scope.parent
        entry = fn.scope.get(name) if fn is not None else None
        info = self._modules.get(module, {})
        if entry is None:
            bound = self._bindings(None, module)
            if name in bound:
                return self._held(bound[name], None, module)
            entry = info.get("names", {}).get(name)
        if entry is not None:
            return self._entry(entry)
        return f"builtin {name}" if name in _BUILTIN else None

    def _entry(self, entry):
        """The kind of a name the index holds: a function, a class, or an import."""
        found = self._resolve_name(entry)
        if isinstance(found, str):
            return f"function {found}"
        if isinstance(found, tuple) and found[0] == "class":
            return f"type {found[1]}"
        if isinstance(found, tuple):
            return self._module_kind(found[1])
        if isinstance(entry, tuple) and entry[0] == "from":
            _, base, name = entry
            if base.split(".")[0] == SQLA:
                return f"{SQLA} {name}"
            if base in self._modules:
                return self._global(base, name)
            return None if base.split(".")[0] in self._roots else f"outside {base}.{name}"
        return None

    def _module_kind(self, name: str) -> str | None:
        if name.split(".")[0] == SQLA:
            return SQLA
        if name in self._modules:
            return f"module {name}"
        return None if name.split(".")[0] in self._roots else f"outside {name}"

    def _global(self, module: str, name: str):
        bound = self._bindings(None, module)
        if name in bound:
            return self._held(bound[name], None, module)
        entry = self._modules[module]["names"].get(name)
        return self._entry(entry) if entry is not None else None

    def _held(self, bindings, fn, module):
        """What a name holds across the values bound to it; an annotation declares it for
        all, except that a write statement bound to it is what it holds whatever it is
        annotated as."""
        declared = [b for b in bindings if b[0] == "annotation"]
        if not declared:
            return _combine(self._bound(b, fn, module) for b in bindings)
        writes = sorted({k for b in bindings if b[0] != "annotation" and (k := self._bound(b, fn, module)) in _SQL_WRITES})
        return writes[0] if writes else _combine(self._bound(b, fn, module) for b in declared)

    def _bound(self, binding, fn, module):
        how, what = binding
        if how == "value":
            return self.kind(what, fn, module)
        if how == "annotation":
            return self._annotation(what, fn, module)
        if how == "entry":
            return self._entry(what)
        if how == "item":  # a loop's target: an item of what it iterates
            held = self.kind(what, fn, module)
            return (held[5:] or None) if held and held.startswith("list:") else _CYCLE if held == _CYCLE else None
        return what  # a kind the binding fixes: "class <key>" for self, None for an unknown value

    def _bindings(self, fn, module=None) -> dict[str, list]:
        """Each name a function (or a module's top level) binds, with what binds it."""
        root = fn.node if fn is not None else self._modules[module]["tree"]
        if id(root) in self._bound_names:
            return self._bound_names[id(root)]
        bound = defaultdict(list)

        def bind(target, binding):
            if isinstance(target, ast.Name):
                bound[target.id].append(binding)
            elif isinstance(target, (ast.Tuple, ast.List)):
                value = binding[1] if binding[0] == "value" else None
                pairs = isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts)
                for i, element in enumerate(target.elts):
                    bind(element, ("value", value.elts[i]) if pairs else ("kind", None))
            elif isinstance(target, ast.Starred):
                bind(target.value, ("kind", "list:"))

        if fn is not None:
            args = fn.node.args
            positional = [*args.posonlyargs, *args.args]
            defaults = dict(zip([a.arg for a in positional][len(positional) - len(args.defaults):], args.defaults))
            defaults.update({a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None})
            static = any(_last(d) == "staticmethod" for d in fn.node.decorator_list)
            for i, arg in enumerate([*positional, *args.kwonlyargs]):
                if i == 0 and fn.cls and not static and arg.arg in ("self", "cls"):
                    bound[arg.arg].append(("kind", f"{'class' if arg.arg == 'self' else 'type'} {fn.module}.{fn.cls}"))
                elif arg.annotation is not None:
                    bound[arg.arg].append(("annotation", arg.annotation))
                elif arg.arg in defaults:
                    bound[arg.arg].append(("value", defaults[arg.arg]))
                else:
                    bound[arg.arg].append(("kind", None))
            for arg, what in ((args.vararg, "list:"), (args.kwarg, "dict:")):
                if arg is not None:
                    bound[arg.arg].append(("kind", what))
        package = self._modules[fn.module if fn is not None else module]["package"]
        for node in _own(root):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    bind(target, ("value", node.value))
            elif isinstance(node, ast.AnnAssign):
                bind(node.target, ("annotation", node.annotation))
                if node.value is not None:
                    bind(node.target, ("value", node.value))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        bind(item.optional_vars, ("value", item.context_expr))
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                bind(node.target, ("item", node.iter))
            elif isinstance(node, ast.NamedExpr):
                bind(node.target, ("value", node.value))
            elif isinstance(node, ast.ExceptHandler) and node.name:
                bound[node.name].append(("kind", "value"))
            elif isinstance(node, (ast.Import, ast.ImportFrom)) and fn is not None:
                names = {}
                self._imports(node, names, package)
                for name, entry in names.items():
                    bound[name].append(("entry", entry))
        self._bound_names[id(root)] = bound
        return bound

    def _annotation(self, node, fn, module):
        """What a value annotated with *node* holds."""
        if isinstance(node, ast.Constant):
            if node.value is None:
                return "none"
            if not isinstance(node.value, str):
                return None
            try:
                node = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return _combine([self._annotation(node.left, fn, module), self._annotation(node.right, fn, module)])
        if isinstance(node, ast.Subscript):
            base = _last(node.value)
            elts = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if base == "Union":
                return _combine(self._annotation(e, fn, module) for e in elts)
            if base in _YIELDS:
                return self._annotation(elts[0], fn, module)
            if base == "Callable":
                return FACTORY if self._annotation(elts[-1], fn, module) == SESSION else "value"
            if _ANNOTATED.get(base) == "dict:":
                return "dict:" + ((self._annotation(elts[-1], fn, module) or "") if len(elts) > 1 else "")
            if _ANNOTATED.get(base) == "list:":
                return "list:" + (_combine(self._annotation(e, fn, module) for e in elts if not
                                           (isinstance(e, ast.Constant) and e.value is Ellipsis)) or "")
            if base in _ANNOTATED:
                return _ANNOTATED[base]
            node = node.value
        if isinstance(node, ast.Name) and node.id in _ANNOTATED:
            return _ANNOTATED[node.id]
        held = self.kind(node, fn, module) if isinstance(node, (ast.Name, ast.Attribute)) else None
        if held is None or held == _CYCLE:
            return None
        if held.startswith(f"{SQLA} "):
            return _SQLA_TYPES.get(held[len(SQLA) + 1:], held)
        if held.startswith("type "):
            return self._instance(held[5:])
        if held.startswith("outside ") and held.rpartition(".")[2] not in ("Any", "TypeVar"):
            return held
        return None

    def _instance(self, key: str) -> str:
        """The kind of an instance of the class *key*: a session, a mapped row, or neither."""
        def bases(key, seen):
            info = self.classes[key]
            for base in info["node"].bases:
                held = self.kind(base, info["parent"], info["module"]) or ""
                if held.startswith("type ") and held[5:] not in seen:
                    seen.add(held[5:])
                    yield from bases(held[5:], seen)
                yield held
        found = set(bases(key, {key}))
        if found & {f"{SQLA} AsyncSession", f"{SQLA} Session"}:
            return SESSION
        if found & {f"{SQLA} DeclarativeBase", f"{SQLA} DeclarativeBaseNoMeta", f"{SQLA} declarative_base()"}:
            return f"row {key}"
        return f"class {key}"

    def _call(self, call: ast.Call, fn, module):
        func = call.func
        if isinstance(func, ast.Attribute):
            held = self.kind(func.value, fn, module)
            if held is None or held == _CYCLE:
                return held
            if not held.startswith(("module ", "type ", "outside ", SQLA)):
                return self._method(held, func.attr, call, fn, module)
        return self._called(self.kind(func, fn, module), call, fn, module)

    def _called(self, callee, call: ast.Call, fn, module):
        """What calling a value of kind *callee* returns."""
        if callee is None or callee == _CYCLE:
            return callee
        args = call.args
        if callee.startswith("builtin "):
            return _BUILTIN[callee[8:]]
        if callee.startswith(f"{SQLA} "):
            name = callee[len(SQLA) + 1:]
            if name == "text":
                held = self._reading(True, lambda: self.kind(args[0], fn, module)) if args else None
                return held if held and held.startswith("statement ") else None
            # A statement built from a data-changing one (select(cte.c.id)) runs its write.
            return self._given_write(call, fn, module) or _SQLA_MADE.get(name, f"{SQLA} {name}()")
        if callee == FACTORY:
            return SESSION
        if callee.startswith("type "):
            return self._instance(callee[5:])
        if callee.startswith("function "):
            return self._returns(callee[9:])
        if callee.startswith("outside "):
            name = callee[8:].rpartition(".")[2]
            if name == "defaultdict":
                inner = self._called(self.kind(args[0], fn, module), ast.Call(args[0], [], []), fn, module) \
                    if args else None
                return "dict:" + (inner or "")
            if name in ("Depends", "Security") and args:
                return self._called(self.kind(args[0], fn, module), ast.Call(args[0], [], []), fn, module)
            return f"{callee}()"
        return None

    def _method(self, held: str, name: str, call: ast.Call, fn, module):
        """What calling method *name* on a value of kind *held* returns."""
        if held.startswith("statement "):
            # update(T).where(..).values(..) stays the statement it was built from, and one
            # given a data-changing statement (.where(x.in_(cte))) runs its write.
            return held if held in _SQL_WRITES else self._given_write(call, fn, module) or held
        if held == ENGINE:
            return SESSION if name in ("connect", "begin") else None
        if held == FACTORY:
            return SESSION if name == "begin" else None
        if held == SESSION:
            if name == "get" and call.args:  # session.get(Model, key): a row of the model, or None
                model = self.kind(call.args[0], fn, module) or ""
                return self._instance(model[5:]) if model.startswith("type ") else None
            return SESSION if name == "connection" else ENGINE if name == "get_bind" else None
        items = call.args[0] if call.args else None
        first = items.elt if isinstance(items, (ast.ListComp, ast.GeneratorExp)) else \
            items.elts[0] if isinstance(items, (ast.List, ast.Tuple)) and items.elts else None
        if held == "value" and name == "join" and first is not None:
            # sep.join([first, ...]) starts with its first item: the SQL it builds is that item's.
            first = self.kind(first, fn, module)
            return first if first and first.startswith("statement ") else "value"
        if held == "set":
            return "set" if name in ("copy", "union", "difference", "intersection", "symmetric_difference") else None
        if held.startswith("dict:"):
            inner = held[5:] or None
            if name == "setdefault" and len(call.args) > 1:
                return self.kind(call.args[1], fn, module)
            if name in ("get", "pop"):
                return _combine([inner, *(self.kind(a, fn, module) for a in call.args[1:2])])
            return held if name == "copy" else f"list:{inner or ''}" if name == "values" else \
                "list:" if name in ("keys", "items") else None
        if held.startswith("list:"):
            return held if name == "copy" else (held[5:] or None) if name == "pop" else None
        if held.startswith(("class ", "row ")):
            return self._called(self._field(held.partition(" ")[2], name), call, fn, module)
        return None

    def _attr(self, held, name: str, fn, module):
        """What attribute *name* of a value of kind *held* holds."""
        if held is None or held == _CYCLE:
            return held
        if held == f"{SQLA} inspect()" and name == "session":
            return SESSION  # inspect(row).session: the session the row belongs to
        if held == SQLA or held.startswith(f"{SQLA} "):
            return f"{SQLA} {name}"
        if held.startswith("module "):
            entry = self._attribute(held[7:], name)
            return self._entry(entry) if entry is not None else self._global(held[7:], name)
        if held.startswith("outside "):
            return f"{held}.{name}"
        if held.startswith("type "):
            return f"{SQLA} table" if name == "__table__" else self._field(held[5:], name)
        if held.startswith(("class ", "row ")):
            return self._field(held.partition(" ")[2], name)
        if held == SESSION:
            return ENGINE if name == "bind" else SESSION if name == "sync_session" else None
        if held == ENGINE:
            return ENGINE if name == "sync_engine" else None
        if held in _SQL_WRITES:
            return held  # a column of a data-changing CTE (moved.c.id) runs its write where it is selected
        return None

    def _given_write(self, call: ast.Call, fn, module) -> str | None:
        """The write statement among what *call* is given, if any (string literals read as text)."""
        return self._reading(False, lambda: next(
            (k for a in (*call.args, *(kw.value for kw in call.keywords)) if (k := self.kind(a, fn, module)) in _SQL_WRITES),
            None))

    def _returns(self, key: str):
        """What calling the function *key* returns (or yields, for a generator)."""
        fn = self.functions[key]

        def compute():
            values = [n.value for n in _own(fn.node) if isinstance(n, (ast.Return, ast.Yield)) and n.value is not None]
            returned = _combine(self.kind(v, fn, fn.module) for v in values) if values else "none"
            if fn.node.returns is None:
                return returned
            # A str or list[str] annotation says less than the SQL the function builds.
            if returned in _SQL_WRITES:  # a write statement returned is what it returns, whatever annotated
                return returned
            annotated = self._annotation(fn.node.returns, fn, fn.module)
            return returned if annotated in ("value", "list:value") and returned and "statement " in returned \
                else annotated
        return self._memo(("returns", key), compute)

    def _field(self, key: str, name: str):
        """What attribute *name* of an instance of class *key* holds: its annotation in the
        class body, its value there, a method or property, or what its methods assign to it."""
        info = self.classes.get(key)
        if info is None:
            return None

        def compute():
            kinds = []
            for node in info["node"].body:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                    kinds.append(self._annotation(node.annotation, info["parent"], info["module"]))
                elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    kinds.append(self.kind(node.value, info["parent"], info["module"]))
                elif isinstance(node, _FUNCTIONS) and node.name == name:
                    method = f"{key}.{name}"
                    if method not in self.functions:
                        continue
                    if any(_last(d) in ("property", "cached_property") for d in node.decorator_list):
                        kinds.append(self._returns(method))
                    else:
                        kinds.append(f"function {method}")
            if kinds:
                return _combine(kinds)
            for method in self._methods(key):
                for node in _own(method.node):
                    targets = node.targets if isinstance(node, ast.Assign) else \
                        [node.target] if isinstance(node, ast.AnnAssign) else []
                    if any(isinstance(t, ast.Attribute) and t.attr == name and isinstance(t.value, ast.Name)
                           and t.value.id == "self" for t in targets):
                        kinds.append(self._annotation(node.annotation, method, method.module)
                                     if isinstance(node, ast.AnnAssign) else self.kind(node.value, method, method.module))
            if kinds:
                return _combine(kinds)
            for base in info["node"].bases:
                held = self.kind(base, info["parent"], info["module"])
                if held and held.startswith("type ") and (found := self._field(held[5:], name)) is not None:
                    return found
            return None
        return self._memo(("field", key, name), compute)

    def _methods(self, key: str) -> list[Function]:
        if self._by_class is None:
            self._by_class = defaultdict(list)
            for fn in self.functions.values():
                if fn.cls:
                    self._by_class[f"{fn.module}.{fn.cls}"].append(fn)
        return self._by_class[key]

    # -- reachability ----------------------------------------------------------

    def closure(self, starts, stop=frozenset()) -> dict[str, tuple[str, int] | None]:
        """Every function the *starts* reach through calls the graph resolves, each with
        the function and line it is first reached from (None for a start). Functions
        named in *stop* are neither entered nor reported."""
        parents: dict[str, tuple[str, int] | None] = {key: None for key in starts}
        frontier = list(parents)
        while frontier:
            following = []
            for current in frontier:
                for line, callee in self.functions[current].calls:
                    if callee not in parents and callee not in stop:
                        parents[callee] = (current, line)
                        following.append(callee)
            frontier = following
        return parents

    def doing(self, starts, wanted, stop=frozenset()) -> list[list[tuple[str, int, str]]]:
        """Each reached function that does something *wanted* accepts itself, as the chain
        of calls to it: [(function key, line, what it calls or does there), ...]."""
        parents = self.closure(starts, stop)
        found = []
        for key in parents:
            for line, what in self.functions[key].effects:
                if wanted(what):
                    chain, step = [(key, line, what)], key
                    while parents[step] is not None:
                        parent, at = parents[step]
                        chain.insert(0, (parent, at, step))
                        step = parent
                    found.append(chain)
        return found


def describe(chain) -> str:
    return " -> ".join(f"{key.rsplit('.', 1)[-1]}:{line} {what.rsplit('.', 1)[-1]}" for key, line, what in chain)
