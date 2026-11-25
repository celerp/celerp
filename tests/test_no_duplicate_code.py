"""One body, one definition; one fact, one writing. No two first-party functions
(celerp/, ui/, default_modules/; tests and migrations aside) share a body once
docstrings and local names are erased, and no string fact restates a named module
constant or repeats an unowned string collection. A copy turns this module red; the
shared owner is imported instead. ALLOWED_DUPLICATES names the groups that are
explained, each with its reason; it only shrinks."""
import ast
import hashlib
from collections import Counter
from functools import lru_cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("celerp", "ui", "default_modules")
_SKIP = {"tests", "migrations", "build", "__pycache__"}

_QUEUED = "pre-existing, collapse queued 2026-11-25"


def _group(*places):
    return frozenset(places)


ALLOWED_DUPLICATES = {
}


class _Normalise(ast.NodeTransformer):
    """Parameter and assigned local names are erased, so a renamed copy still
    matches; module names, attributes and called helpers are kept, so two bodies
    over different facts differ."""

    def __init__(self, local):
        self.local = local

    def visit_Name(self, node):
        if node.id in self.local:
            return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)
        return node

    def visit_arg(self, node):
        return ast.copy_location(ast.arg(arg="_", annotation=None), node)


def _body_hash(function):
    """Hash of a function body with its docstring and local names removed. No
    length threshold: two-line wrappers are compared like any other body."""
    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    local = {arg.arg for arg in ast.walk(function.args) if isinstance(arg, ast.arg)}
    local |= {node.id for statement in body for node in ast.walk(statement)
              if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))}
    normalise = _Normalise(local)
    module = ast.Module(body=[normalise.visit(ast.parse(ast.unparse(s)).body[0]) for s in body],
                        type_ignores=[])
    return hashlib.sha1(ast.dump(module).encode()).hexdigest()


def _functions(node, prefix=""):
    """(qualified name, node) of every function, methods and nested ones included."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = f"{prefix}{child.name}"
            if not isinstance(child, ast.ClassDef):
                yield name, child
            yield from _functions(child, f"{name}.")
        else:
            yield from _functions(child, prefix)


@lru_cache(maxsize=None)
def _modules():
    """(repo-relative path, tree) of every first-party module."""
    return tuple((path.relative_to(REPO).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
                 for root in ROOTS for path in sorted((REPO / root).rglob("*.py"))
                 if not _SKIP.intersection(path.relative_to(REPO).parts))


@lru_cache(maxsize=None)
def _function_hashes():
    hashes = {}
    for rel, tree in _modules():
        for name, node in _functions(tree):
            hashes.setdefault(_body_hash(node), []).append(f"{rel}::{name}")
    return hashes


def _duplicates(groups):
    return {frozenset(locations) for locations in groups.values() if len(locations) > 1}


_COLLECTIONS = {"frozenset": "set", "set": "set", "tuple": "seq", "list": "seq"}


def _value_key(value):
    """Typed, order-free for sets: 1, 1.0 and True stay different; {a, b} equals {b, a}."""
    if isinstance(value, (set, frozenset)):
        return ("set",) + tuple(sorted((_value_key(item) for item in value), key=repr))
    if isinstance(value, (list, tuple)):
        return ("seq",) + tuple(_value_key(item) for item in value)
    if isinstance(value, dict):
        return ("dict",) + tuple(sorted(((_value_key(k), _value_key(v)) for k, v in value.items()),
                                        key=repr))
    return (type(value).__name__, value)


def _literal_key(node):
    """Canonical value of a literal, of frozenset/set/tuple/list over one, or of re.compile and
    Decimal calls on literals; None for anything computed."""
    if isinstance(node, ast.Call):
        name = ast.unparse(node.func)
        args = [_literal_key(arg) for arg in node.args]
        if node.keywords or not args or None in args:
            return None
        if name in _COLLECTIONS and len(args) == 1 and args[0][0] in ("set", "seq"):
            return (_COLLECTIONS[name],) + (tuple(sorted(args[0][1:], key=repr))
                                            if _COLLECTIONS[name] == "set" else args[0][1:])
        return (name,) + tuple(args) if name in ("re.compile", "Decimal") else None
    try:
        return _value_key(ast.literal_eval(node))
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _held(key):
    """The place-set member that ties a restated-literal group to its value, so an allowlist
    entry covers that one value and no other written in the same places."""
    return f"value {key!r}"


def _named_constants(tree):
    """(name, value node) of every module-level assignment to an upper-case name."""
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id.lstrip("_").isupper():
                yield target.id, statement.value


def _constants_by_value(rel, tree):
    """(key, rel::name) of every named constant with a literal value."""
    return [(key, f"{rel}::{name}") for name, value in _named_constants(tree)
            if (key := _literal_key(value)) is not None]


# Shorter strings are vocabulary tokens (status words, currency codes, short keys)
# shared by unrelated facts.
_FACT_STRING_MIN = 5


def _is_string_fact(key):
    if key[0] in ("set", "seq"):
        return len(key) > 2 and all(item[0] == "str" for item in key[1:])
    return key[0] == "re.compile" or (key[0] == "str" and len(key[1]) >= _FACT_STRING_MIN)


def _members(key):
    """A string collection is one fact whatever its container: its sorted unique members."""
    if key[0] in ("set", "seq") and _is_string_fact(key):
        return ("set",) + tuple(sorted(set(key[1:]), key=repr))
    return key


def _by_members(entries):
    """Places grouped by value, string collections compared by members."""
    groups = {}
    for key, place in entries:
        groups.setdefault(_members(key), []).append(place)
    return groups


class _Literals(ast.NodeVisitor):
    """Every string fact written as a literal at any depth (inline, local, class level, lowercase
    name, or inside the value of a named constant, a string collection or an f-string),
    keyed by value with its enclosing scope. Not recorded: the nodes in `owned` (a named
    constant's own value, the literal a frozenset, set, tuple or list call wraps, and
    docstrings)."""

    def __init__(self, rel, owned):
        self.rel, self.owned, self.scope, self.found = rel, owned, [], []

    def _own_docstring(self, node):
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                node.body[0].value, ast.Constant):
            self.owned.add(id(node.body[0].value))

    def visit_Module(self, node):
        self._own_docstring(node)
        self.generic_visit(node)

    def _enter(self, node):
        self._own_docstring(node)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

    def generic_visit(self, node):
        if isinstance(node, ast.expr):
            key = _literal_key(node)
            if key is not None and isinstance(node, ast.Call) and ast.unparse(
                    node.func) in _COLLECTIONS:
                self.owned.add(id(node.args[0]))  # frozenset({...}) is one writing of one value
            if key is not None and _is_string_fact(key) and id(node) not in self.owned:
                self.found.append((key, f"{self.rel}::{'.'.join(self.scope) or '<module>'}"))
        super().generic_visit(node)


def _written_literals(rel, tree):
    """(key, rel::scope) of every string fact written in one module."""
    visitor = _Literals(str(rel), {id(value) for _, value in _named_constants(tree)})
    visitor.visit(tree)
    return visitor.found


def _restated_groups(named, occurrences):
    """A string fact equal to a named module constant, or a string collection written twice
    with no named owner: the value, the constant's name and every place it is written again,
    with its count where one place writes it more than once (so a new copy is a new group)."""
    groups = set()
    for key, places in occurrences.items():
        counted = {place if n == 1 else f"{place} x{n}" for place, n in Counter(places).items()}
        if key in named:
            groups.add(frozenset(named[key]) | counted | {_held(key)})
        elif key[0] == "set" and len(places) > 1:
            groups.add(frozenset(counted) | {_held(key)})
    return groups


@lru_cache(maxsize=None)
def _restated_literals():
    named = _by_members(entry for rel, tree in _modules() for entry in _constants_by_value(rel, tree))
    written = _by_members(entry for rel, tree in _modules() for entry in _written_literals(rel, tree))
    return frozenset(_restated_groups(named, written))


def test_no_two_functions_share_a_body():
    unexplained = _duplicates(_function_hashes()) - ALLOWED_DUPLICATES.keys()
    assert not unexplained, f"one body, several definitions: {sorted(map(sorted, unexplained))}"


def test_no_literal_restates_a_named_constant_or_an_unowned_collection():
    unexplained = _restated_literals() - ALLOWED_DUPLICATES.keys()
    assert not unexplained, f"one fact, written again: {sorted(map(sorted, unexplained))}"


def test_every_allowed_duplicate_is_still_a_live_group():
    stale = ALLOWED_DUPLICATES.keys() - (_duplicates(_function_hashes()) | _restated_literals())
    assert not stale, sorted(map(sorted, stale))


@pytest.mark.parametrize("source, same", [
    ("def a(x):\n    return x + LIMIT\ndef b(y):\n    return y + LIMIT\n", True),
    ("def a(x):\n    return x + LIMIT\ndef b(y):\n    return y + FLOOR\n", False),
    ("def a(x):\n    raise ValueError('a')\ndef b(x):\n    raise ValueError('b')\n", False),
])
def test_the_body_hash_erases_local_names_only(source, same):
    first, second = ast.parse(source).body
    assert (_body_hash(first) == _body_hash(second)) is same


@pytest.mark.parametrize("first, second, same", [
    ('frozenset({"A", "B"})', '{"B", "A"}', True),
    ('re.compile(r"[0-9a-f]{40}\\Z")', 're.compile(r"[0-9a-f]{40}\\Z")', True),
    ('Decimal("100")', 'Decimal("100")', True),
    ("1", "1.0", False),
    ("1", "True", False),
    ('("A", "B")', '("B", "A")', False),
])
def test_the_value_key_matches_equal_literals_in_any_spelling(first, second, same):
    keys = [_literal_key(ast.parse(text, mode="eval").body) for text in (first, second)]
    assert None not in keys and (keys[0] == keys[1]) is same


_PAIR = {"AAAAA", "BBBBB"}


@pytest.mark.parametrize("source, written", [
    ('T = {"fx": {"AAAAA", "BBBBB"}}', [_PAIR, "AAAAA", "BBBBB"]),
    ('T = sorted({"AAAAA", "BBBBB"})', [_PAIR, "AAAAA", "BBBBB"]),
    ('def g(s):\n    return f"label text {s in {\'AAAAA\', \'BBBBB\'}}"',
     ["label text ", _PAIR, "AAAAA", "BBBBB"]),
    ('U = "x-" + "agent string"', ["agent string"]),
    ('T = frozenset({"AAAAA", "BBBBB"})', ["AAAAA", "BBBBB"]),
    ('def g():\n    return frozenset({"AAAAA", "BBBBB"})', [_PAIR, "AAAAA", "BBBBB"]),
    ('"""A module docstring."""', []),
])
def test_the_literal_walk_records_every_depth_but_a_constant_own_value(source, written):
    found = _written_literals("m.py", ast.parse(source))
    assert sorted(key for key, _ in found) == sorted(map(_value_key, written))


_STATES = '"PLANNED", "RESERVED", "POSTED", "WORKING", "VOIDED"'
_OWNERS = (f"STATES = frozenset({{{_STATES}}})\nAGENT = 'agent string'\nEPISODE = 'rates_2022'\n"
           "EPISODES = {'rates_2022': 1}\n")
_USE = "def g(item):\n    return item in STATES or AGENT\n"


def _guard_groups(source):
    tree = ast.parse(_OWNERS + source)
    return _restated_groups(_by_members(_constants_by_value("m.py", tree)),
                            _by_members(_written_literals("m.py", tree)))


@pytest.mark.parametrize("source", [
    _USE.replace("AGENT", "f'agent string'"),
    _USE.replace("AGENT", "f\"{''}agent string\""),
    _USE.replace("STATES", f"({_STATES})"),
    _USE.replace("STATES", f"[{_STATES}]"),
    _USE + f"def h(table):\n    return table[{_STATES}]\n",
    _USE + "_again = ['rates_2022', 'x']\n",
    _USE.replace("STATES", f"{{*({_STATES})}}"),
], ids=["f-string", "f-string-text", "tuple", "list", "subscript", "second-copy-in-place", "starred"])
def test_a_restated_fact_in_any_spelling_is_a_new_group(source):
    assert _guard_groups(source) - _guard_groups(_USE)
