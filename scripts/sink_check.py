#!/usr/bin/env python3
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Sink check: raw markup is built only inside the encoder module; every other raw door is refused.

Usage: sink_check.py <repo-root>
Exit code: number of hits (0 = clean). One line per hit: CODE | path:line | snippet.

SCOPE (declared): every tracked *.py file that is not a test (tests/ dirs, test_*.py,
conftest*.py) or an Alembic migration, and every tracked browser *.js the app serves or
loads (ui/static, electron/assets, electron/preload.js), excluding vendored third-party
bundles (*.min.js, ui/static/vendor/). Python is parsed with ast; names are matched after
import resolution (from-imports, module imports, star imports from a framework, and
local or module aliases such as S = Script), so an alias names the same primitive as the
original. Every Python string constant (f-string literal parts joined with a hole
marker) and every served .js file is tokenized as JavaScript and scanned for the
browser sink list.

THE INVARIANT: the raw primitives are named only in the encoder module.
  PY-RAW       an import or reference of NotStr, Safe, Markup, __html__, to_xml, to_html,
               HTMLResponse or Js (raw and Markup when imported from a framework), any
               do_escape other than the constant True, a def __html__; raw_html(...) or
               js(...) whose template is not a constant; a lowercase tag builder (ft, ft_hx,
               ft_html) with a computed tag; a computed attribute NAME (**{k: v}, a mapping
               child with a computed key, el.attrs[k] = v); a bytes child (.encode(),
               bytes(), bytearray()); a mapping child read from a mapping parameter; a
               non-constant srcdoc; a returned string that builds markup (a "<tag" literal
               with a hole), which FastHTML would send as an HTML response
  PY-POSITION  a raw_html template hole in a URL attribute (href, src, action, formaction,
               srcset, poster, data, xlink:href), an on* or hx-on* attribute, srcdoc, style,
               an htmx-evaluated attribute (hx-vals, hx-headers, hx-trigger), an attribute
               name, an unquoted value, a comment, or <script>/<style> text; a js template
               hole inside a JS string, template literal or comment token
  PY-COMPUTED  getattr/setattr/vars/__dict__/import_module with a computed name on a
               module from fasthtml, fastcore, starlette or fastapi
  PY-HTMLRESP  Response/StreamingResponse/FileResponse whose media type is html or not a
               constant (inert_media_type(...) excepted), a FileResponse of a computed path
               with no media type, a content-type header that is html or not a constant,
               and any write after construction: .body, .media_type or .raw_headers
               assignment, headers[...] = with a content-type or computed key,
               headers.update/setdefault with a content-type key or a computed mapping,
               init_headers(...)
  PY-JSHOLE    a non-constant value in a script position (Script(...) or ft("script"),
               on*/hx_on* keyword or "on*"/"hx-on*" key, set before or after construction,
               hx_vals/hx_headers starting "js:", a hole inside an hx_trigger filter) that
               is not js_value(...), js(constant_template, ...) or as_js(...)
  PY-CSSHOLE   a non-constant value in Style(...), ft("style") or a style attribute, other
               than a numeric expression
  PY-URLHOLE   a non-constant URL attribute value, before or after construction, that
               neither starts with a literal same-origin path ("/x", "?", "#") nor is
               safe_url(...); a meta refresh with a non-constant content
  JS-HTML      write to innerHTML/outerHTML/srcdoc; insertAdjacentHTML, createContextualFragment,
               setHTMLUnsafe, parseHTMLUnsafe, execCommand('insertHTML'), htmx.swap and
               jQuery-style .html(x) calls; write/writeln on document, contentDocument,
               ownerDocument or a name bound to document
  JS-SCRIPT    createElement('script'); eval, Function, importScripts; import(x), new Worker,
               new SharedWorker or serviceWorker.register with a non-constant argument;
               setTimeout or setInterval whose first argument is not a function; a string
               on* handler
  JS-NAV       write to location/href/src/action/formAction; location.assign/replace;
               window.open/open; setAttribute or setAttributeNS of a URL or on* name
  JS-COMPUTED  computed member access with a sink name, setAttribute with a computed
               name, computed member assignment, a sink name as an object-literal key, or
               an innerHTML/outerHTML/srcdoc string passed as an argument (Reflect.set)
  JS-SCHEME    a javascript:, vbscript: or data:text/html URL literal

There is no allowlist. The exemption is the encoder module, one file per language:
ui/components/encode.py (raw_html, js, js_value, safe_url, inert_media_type, to_markup,
html_response, library_markup; nothing in it is reported) and ui/static/celerp-dom.js
(celerpNode, celerpSetUrl, celerpGo; its JS-HTML, JS-NAV and JS-COMPUTED hits are not
reported). hole_positions() and the JavaScript token pattern are loaded from encode.py, so
raw_html and js at run time and this check share one position parser.
"""
from __future__ import annotations

import ast
import copy
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

_APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_APP))
_spec = importlib.util.spec_from_file_location("_celerp_encode", _APP / "ui" / "components" / "encode.py")
_encode = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_encode)
HOLE, EVENT_ATTR_RE, URL_ATTRS, HTMX_JS_ATTRS, TOKEN_RE, hole_positions = (
    _encode.HOLE, _encode.EVENT_ATTR_RE, _encode.URL_ATTRS, _encode.HTMX_JS_ATTRS,
    _encode.TOKEN_RE, _encode.hole_positions)

FRAMEWORK_PREFIXES = ("fasthtml", "fastcore", "starlette", "fastapi")
ENCODER_PATH = "ui/components/encode.py"
ADMISSION_JS = "ui/static/celerp-dom.js"  # celerpNode, celerpSetUrl, celerpGo
RAW_PRIMS = {"NotStr", "Safe", "Markup", "__html__", "to_xml", "to_html", "HTMLResponse", "Js"}
RAW_FRAMEWORK_ONLY = {"raw"}  # an ordinary word: refused only when imported from a framework
# A name as a string feeds getattr laundering. Compared against or listed in a set literal
# it cannot reach the object (a set only reaches it through a computed lookup, reported).
RAW_STRINGS = {"NotStr", "Safe", "Markup", "__html__"}
ENCODERS_JS = {"js_value", "js", "as_js"}
ENCODERS_URL = {"safe_url"}
LOWER_BUILDERS = {"ft", "ft_hx", "ft_html", "ft_datastar"}
RESPONSES = {"Response", "StreamingResponse", "FileResponse"}
URL_ATTR_TAG = {"data": "object", "poster": "video"}  # these names are URLs only on one element
NUMERIC_CALLS = {"int", "len", "round", "float", "bool", "abs", "min", "max", "sum"}
JS_LIKE_RE = re.compile(r"\b(document|window|location|this|self|el|e|evt|event|htmx|fetch)\.|"
                        r"\bfunction\s*\(|=>|\)\s*;|\b(var|let|const)\s+\w+\s*=|:\s*\S*javascript\s*:", re.I)
HTML_TAGS = ("a|abbr|b|body|br|button|code|div|em|form|h[1-6]|head|hr|html|i|iframe|img|input|label|li|link|"
             "meta|ol|option|p|pre|script|select|small|span|strong|style|svg|table|tbody|td|textarea|th|"
             "thead|title|tr|u|ul|!doctype")
MARKUP_RE = re.compile(rf"<(/?(?:{HTML_TAGS}))[\s>/]", re.I)
UI_ROOTS = ("ui/", "default_modules/")  # where in-repo components wrap elements; core has none
LOCAL_TOPS: set[str] = set()  # top-level packages of the scanned repo
MODULE_CONSTS: dict[str, dict[str, ast.AST]] = {}


def _inline_consts(table: dict[str, ast.AST]) -> dict[str, ast.AST]:
    """Each module-level value with the module's other module-level names inlined, so a module
    that imports it resolves it without the defining module's scope."""
    class Inline(ast.NodeTransformer):
        def __init__(self, depth):
            self.depth = depth

        def visit_Name(self, node):
            if node.id in table and self.depth < 8:
                return Inline(self.depth + 1).visit(copy.deepcopy(table[node.id]))
            return node

    return {k: Inline(0).visit(copy.deepcopy(v)) for k, v in table.items()}
MODULE_FUNCS: dict[str, dict[str, ast.AST]] = {}
DYNAMIC_MODULES: dict[str, set[str]] = {}  # names bound to import_module(...) results, per file


def _framework_import(node) -> bool:
    """import_module("fasthtml...") or __import__("starlette...") with a constant framework name."""
    return (isinstance(node, ast.Call) and _call_name(node) in ("import_module", "__import__")
            and bool(node.args) and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).startswith(FRAMEWORK_PREFIXES))


def module_name(path: str) -> str:
    parts = list(Path(path).with_suffix("").parts)
    if parts[0] == "default_modules" and len(parts) > 2:
        parts = parts[2:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


HTML_SINK_PROPS = {"innerHTML", "outerHTML", "srcdoc"}
NAV_PROPS = {"location", "href", "src", "action", "formAction"}
ALL_SINK_NAMES = HTML_SINK_PROPS | NAV_PROPS | {
    "insertAdjacentHTML", "write", "writeln", "createContextualFragment", "eval",
    "Function", "setTimeout", "setInterval", "importScripts", "assign", "replace",
    "open", "setAttribute", "setAttributeNS", "text", "setHTMLUnsafe", "parseHTMLUnsafe"}
SCHEME_RE = re.compile(r"^\s*(javascript|vbscript)\s*:|^\s*data\s*:\s*text/html", re.I)
SAFE_PATH_RE = re.compile(r"^(/[^/\\]|\?|#)")

hits: list[tuple[str, str, int, str]] = []


def hit(code, path, line, snippet):
    hits.append((code, path, line, " ".join(str(snippet).split())[:140]))


# ---------------------------------------------------------------- JavaScript
def js_tokens(src: str):
    out = []
    line = 0
    for m in TOKEN_RE.finditer(src):
        kind = m.lastgroup
        text = m.group()
        if kind not in ("ws", "lc", "bc"):
            out.append((kind, text, line))
        line += text.count("\n")
    return out


def _strval(tok):
    return tok[1][1:-1] if tok[0] == "str" else None


def scan_js(src: str, path: str, base_line: int):
    t = js_tokens(src)
    n = len(t)
    declared = set()
    for i, (k, v, _) in enumerate(t):
        if k == "id" and v == "function" and i + 1 < n and t[i + 1][0] == "id":
            declared.add(t[i + 1][1])
        if k == "id" and i + 2 < n and t[i + 1][1] == "=" and (
                t[i + 2][1] in ("function", "async", "(") or (i + 3 < n and t[i + 3][1] == "=>")):
            declared.add(v)

    doc_alias = {"document", "contentDocument", "ownerDocument"}
    for i, (k, v, _) in enumerate(t):
        if k == "id" and i + 2 < n and t[i + 1][1] == "=" and t[i + 2][1] in doc_alias \
                and (i + 3 >= n or t[i + 3][1] != "."):
            doc_alias.add(v)

    plain = set()  # identifiers bound to plain objects, arrays or maps in this source
    for i, (k, v, _) in enumerate(t):
        if k == "id" and i + 2 < n and t[i + 1][1] == "=" and (
                t[i + 2][1] in ("{", "[") or t[i + 2][1] in ("Object", "JSON", "new")):
            plain.add(v)
    assign_spans = []  # token ranges of Object.assign(...) arguments
    for i in range(n - 3):
        if t[i][1] == "Object" and t[i + 1][1] == "." and t[i + 2][1] == "assign" and t[i + 3][1] == "(":
            depth, j = 0, i + 3
            while j < n:
                depth += t[j][1] == "("
                depth -= t[j][1] == ")"
                if depth == 0:
                    break
                j += 1
            assign_spans.append((i + 3, j))

    def in_assign(i):
        return any(a < i < b for a, b in assign_spans)

    def at(i):
        return t[i] if 0 <= i < n else ("", "", 0)

    def is_assign(i):
        return at(i)[1] in ("=", "+=")

    def report(code, i):
        lo, hi = max(0, i - 4), min(n, i + 6)
        hit(code, path, base_line + t[i][2], " ".join(x[1] for x in t[lo:hi]))

    for i, (k, v, _) in enumerate(t):
        prev, nxt = at(i - 1)[1], at(i + 1)[1]
        if k == "str":
            sv = _strval(t[i])
            if sv and SCHEME_RE.search(sv):
                report("JS-SCHEME", i)
            if sv in ALL_SINK_NAMES and prev == "[":
                report("JS-COMPUTED", i)
            elif sv in HTML_SINK_PROPS and prev in ("(", ",") and at(i - 2)[1] not in ("setAttribute",
                                                                                     "getAttribute"):
                report("JS-COMPUTED", i)
            if nxt == ":" and prev in ("{", ",") and (sv in HTML_SINK_PROPS or (sv in NAV_PROPS and in_assign(i))):
                report("JS-COMPUTED", i)
            continue
        if k == "op" and v == "[" and prev not in ("(", ",", "=", "[", ":", "return", "") \
                and at(i - 1)[0] in ("id", "op") and prev not in ("+", "-", "?", "&&", "||"):
            # computed member assignment: x[expr] = ... with a non-numeric key
            depth, j = 0, i
            while j < n:
                if t[j][1] == "[":
                    depth += 1
                elif t[j][1] == "]":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            key = t[i + 1:j]
            if j < n and is_assign(j + 1) and not (len(key) == 1 and key[0][0] in ("num",)) \
                    and not (at(i - 1)[0] == "id" and at(i - 2)[1] != "." and prev in plain):
                report("JS-COMPUTED", i)
            continue
        if k != "id":
            continue
        member = prev == "."
        if v in HTML_SINK_PROPS and member and is_assign(i + 1):
            report("JS-HTML", i)
        elif v in HTML_SINK_PROPS and nxt == ":" and prev in ("{", ","):
            report("JS-COMPUTED", i)
        elif v in ("insertAdjacentHTML", "createContextualFragment") and member and nxt == "(":
            report("JS-HTML", i)
        elif v in ("write", "writeln") and member and at(i - 2)[1] in doc_alias and nxt == "(":
            report("JS-HTML", i)
        elif v in ("setHTMLUnsafe", "parseHTMLUnsafe") and member and nxt == "(":
            report("JS-HTML", i)
        elif v == "execCommand" and nxt == "(" and (_strval(at(i + 2)) or "").lower() == "inserthtml":
            report("JS-HTML", i)
        elif v == "swap" and member and at(i - 2)[1] == "htmx" and nxt == "(":
            report("JS-HTML", i)
        elif v == "html" and member and nxt == "(" and at(i + 2)[1] != ")":
            report("JS-HTML", i)
        elif v == "import" and nxt == "(" and not (at(i + 2)[0] == "str" and at(i + 3)[1] == ")"):
            report("JS-SCRIPT", i)
        elif (v in ("Worker", "SharedWorker") and prev == "new" or
              v == "register" and member and at(i - 2)[1] == "serviceWorker") and nxt == "(" \
                and not (at(i + 2)[0] == "str" and at(i + 3)[1] in (")", ",")):
            report("JS-SCRIPT", i)
        elif v == "createElement" and nxt == "(" and (_strval(at(i + 2)) or "").lower() == "script":
            report("JS-SCRIPT", i)
        elif v in ("eval", "Function", "importScripts") and nxt == "(" and prev != "function":
            report("JS-SCRIPT", i)
        elif v in ("setTimeout", "setInterval") and nxt == "(":
            a, b = at(i + 2), at(i + 3)
            ok = a[1] in ("function", "async", "(") or (a[0] == "id" and a[1] in declared
                                                          and b[1] in (",", ")"))
            if a[0] == "id" and b[1] == "=>":
                ok = True
            if not ok:
                report("JS-SCRIPT", i)
        elif v.startswith("on") and len(v) > 2 and member and is_assign(i + 1) and at(i + 2)[0] == "str":
            report("JS-SCRIPT", i)
        elif v in NAV_PROPS and is_assign(i + 1) and (member or v == "location"):
            report("JS-NAV", i)
        elif v in NAV_PROPS and nxt == ":" and prev in ("{", ",") and in_assign(i):
            report("JS-COMPUTED", i)
        elif v in ("assign", "replace") and member and at(i - 2)[1] == "location" and nxt == "(":
            report("JS-NAV", i)
        elif v == "open" and nxt == "(" and ((not member and v not in declared and prev != "function")
                                             or (member and at(i - 2)[1] in ("window", "self", "top", "parent"))):
            report("JS-NAV", i)
        elif v in ("setAttribute", "setAttributeNS") and member and nxt == "(":
            arg = at(i + 2) if v == "setAttribute" else at(i + 4)
            name = (_strval(arg) or "").lower()
            if arg[0] != "str" or at(i + 3 if v == "setAttribute" else i + 5)[1] != ",":
                report("JS-COMPUTED", i)
            elif name in URL_ATTRS or name == "srcdoc" or name.startswith("on"):
                report("JS-NAV", i)


# ---------------------------------------------------------------- Python
def _const_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else HOLE for v in node.values)
    return None


def _call_name(node):
    if isinstance(node, ast.Call):
        f = node.func
        return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
    return None


def _key(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


MAPPING_ANN = {"dict", "Dict", "Mapping", "MutableMapping"}
DATA_BASES = {"NamedTuple", "TypedDict", "Enum", "StrEnum", "IntEnum"}


class PyScan(ast.NodeVisitor):
    def __init__(self, path, src):
        self.path, self.src = path, src
        self.origin: dict[str, str] = {}  # local name -> qualified origin
        self.star = False  # a star import from a framework module
        self.consts: dict[str, ast.AST] = {}
        self.imported: dict[str, tuple[str, str]] = {}
        self.funcs = MODULE_FUNCS.get(module_name(path), {})
        self.locals: list[dict[str, ast.AST]] = []
        self.mapping_params: list[set[str]] = []
        self.classes: set[str] = set()  # module classes on a data base only: values, not elements

    def snip(self, node):
        return (ast.get_source_segment(self.src, node) or "")[:140]

    # ------------------------------------------------ import resolution
    def qual(self, node, depth=0):
        """Qualified origin of a name or attribute chain, after imports and aliases; None if unknown."""
        if depth > 8:
            return None
        if isinstance(node, ast.Name):
            for scope in reversed(self.locals):
                if node.id in scope:
                    return self.qual(scope[node.id], depth + 1)
            if node.id in self.origin:
                return self.origin[node.id]
            if node.id in self.consts:
                return self.qual(self.consts[node.id], depth + 1)
            if self.star and node.id in RAW_PRIMS | LOWER_BUILDERS | RESPONSES:
                return f"fasthtml.common.{node.id}"
            return None
        if isinstance(node, ast.Attribute):
            base = self.qual(node.value, depth + 1)
            return f"{base}.{node.attr}" if base else None
        return None

    def leaf(self, node):
        """The primitive a callee names: the import-resolved leaf, else the written name."""
        q = self.qual(node)
        if q:
            return q.rsplit(".", 1)[-1]
        return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None

    def framework(self, node):
        q = self.qual(node)
        return bool(q) and q.startswith(FRAMEWORK_PREFIXES)

    def is_raw_prim(self, name, origin):
        return name in RAW_PRIMS or (name in RAW_FRAMEWORK_ONLY and origin.startswith(FRAMEWORK_PREFIXES))

    def visit_Module(self, node):
        for st in node.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                self.consts[st.targets[0].id] = st.value
            elif isinstance(st, ast.ClassDef) and all(self.leaf(b) in DATA_BASES for b in st.bases):
                self.classes.add(st.name)
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            local = a.asname or a.name.split(".")[0]
            self.origin[local] = a.name if a.asname else a.name.split(".")[0]
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        mod = "." * node.level + (node.module or "")
        for a in node.names:
            if a.name == "*":
                self.star = self.star or mod.startswith(FRAMEWORK_PREFIXES)
                continue
            self.origin[a.asname or a.name] = f"{mod}.{a.name}"
            if not mod.startswith(FRAMEWORK_PREFIXES):
                self.imported[a.asname or a.name] = (mod, a.name)
            if self.is_raw_prim(a.name, mod):
                hit("PY-RAW", self.path, node.lineno, f"from {mod} import {a.name}")
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and node.id not in self.origin and self.star \
                and node.id in RAW_PRIMS and self.qual(node):
            hit("PY-RAW", self.path, node.lineno, self.snip(node))
        self.generic_visit(node)

    def visit_Attribute(self, node):
        base = self.qual(node.value)
        if node.attr == "__html__" or (base and self.is_raw_prim(node.attr, base) and
                                       (base.startswith(FRAMEWORK_PREFIXES) or node.attr in RAW_PRIMS)):
            hit("PY-RAW", self.path, node.lineno, self.snip(node))
        if node.attr == "__dict__" and self.framework(node.value):
            hit("PY-COMPUTED", self.path, node.lineno, self.snip(node))
        self.generic_visit(node)

    # ------------------------------------------------ scopes
    def visit_FunctionDef(self, node):
        if node.name == "__html__":
            hit("PY-RAW", self.path, node.lineno, "def __html__")
        counts: dict[str, int] = {}
        found: dict[str, ast.AST] = {}
        for sub in ast.walk(node):
            targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target] if isinstance(
                sub, (ast.AugAssign, ast.AnnAssign, ast.For, ast.NamedExpr)) else []
            for tg in targets:
                if isinstance(tg, ast.Name):
                    counts[tg.id] = counts.get(tg.id, 0) + 1
                    if isinstance(sub, ast.Assign):
                        found[tg.id] = sub.value
        args = node.args.args + node.args.kwonlyargs + node.args.posonlyargs
        for a in args:
            counts[a.arg] = 2  # a parameter is never a constant
        self.locals.append({k: v for k, v in found.items() if counts.get(k) == 1})
        self.mapping_params.append({a.arg for a in args if a.annotation is not None and (
            (isinstance(a.annotation, ast.Name) and a.annotation.id in MAPPING_ANN) or
            (isinstance(a.annotation, ast.Subscript) and isinstance(a.annotation.value, ast.Name)
             and a.annotation.value.id in MAPPING_ANN))})
        self.generic_visit(node)
        self.locals.pop()
        self.mapping_params.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    # ------------------------------------------------ strings
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            if node.value in RAW_STRINGS and not getattr(node, "_inert_name", False):
                hit("PY-RAW", self.path, node.lineno, repr(node.value))
            if SCHEME_RE.search(node.value):
                hit("JS-SCHEME", self.path, node.lineno, repr(node.value))
            if not getattr(node, "_in_fstring", False):
                self._scan_js_text(node.value, node.lineno)

    def visit_Compare(self, node):
        for v in (node.left, *node.comparators):
            v._inert_name = True
        self.generic_visit(node)

    def visit_Set(self, node):
        for v in node.elts:
            v._inert_name = True
        self.generic_visit(node)

    def visit_JoinedStr(self, node):
        for v in node.values:
            if isinstance(v, ast.Constant):
                v._in_fstring = True
        self._scan_js_text(_const_text(node), node.lineno)
        self.generic_visit(node)

    def _scan_js_text(self, text, line):
        if text and JS_LIKE_RE.search(text):
            scan_js(text, self.path, line)

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return  # a docstring or bare string statement is never sent to a page
        self.generic_visit(node)

    def visit_Return(self, node):
        if node.value is not None and self._builds_markup(node.value):
            hit("PY-RAW", self.path, node.lineno, self.snip(node.value))
        self.generic_visit(node)

    def _builds_markup(self, node):
        """A str built with a hole whose literal text opens a tag: f-string, %, .format or +."""
        if isinstance(node, ast.JoinedStr):
            text = _const_text(node)
            return MARKUP_RE.search(text.replace(HOLE, "")) is not None and any(isinstance(v, ast.FormattedValue) and not self._is_const(v.value) for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            parts = [node.left, node.right]
            lits = [p.value for p in parts if isinstance(p, ast.Constant) and isinstance(p.value, str)]
            return any(MARKUP_RE.search(x) for x in lits) and not self._is_const(node)
        if _call_name(node) == "format" and isinstance(node.func, ast.Attribute):
            t = _key(node.func.value)
            return bool(t) and MARKUP_RE.search(t) is not None and bool(node.args or node.keywords)
        return False

    # ------------------------------------------------ writes after construction
    def visit_Assign(self, node):
        for tg in node.targets:
            self._check_write(tg, node.value, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._check_write(node.target, node.value, node.lineno)
        self.generic_visit(node)

    def _check_write(self, tg, value, line):
        if isinstance(tg, ast.Attribute):
            if tg.attr in ("body", "media_type", "raw_headers") and self._response_like(tg.value):
                hit("PY-HTMLRESP", self.path, line, self.snip(tg))
            elif EVENT_ATTR_RE.match(tg.attr) or self._element_value(tg.value):
                self._check_attr(tg.attr, value)
        elif isinstance(tg, ast.Subscript) and isinstance(tg.value, ast.Attribute):
            key = _key(self._resolve(tg.slice))
            if tg.value.attr == "headers" and (key is None or key.lower() == "content-type"):
                hit("PY-HTMLRESP", self.path, line, self.snip(tg))
            elif tg.value.attr == "attrs":
                if key is None:
                    hit("PY-RAW", self.path, line, self.snip(tg))
                else:
                    self._check_attr(key, value)

    def _element_value(self, node):
        """A name bound here to an element built by a builder call."""
        bound = self._resolve(node)
        return isinstance(bound, ast.Call) and self._tag_of_quiet(bound) is not None

    def _response_like(self, node):
        """A receiver that may be a response: anything but a name bound to a non-response call here."""
        bound = self._resolve(node)
        if isinstance(bound, ast.Call):
            return self.leaf(bound.func) in RESPONSES | {"PlainTextResponse", "JSONResponse",
                                                         "RedirectResponse", "HTMLResponse"}
        return isinstance(node, ast.Name) and node.id in ("r", "resp", "response", "res") or \
            (isinstance(node, ast.Attribute) and node.attr in ("response", "resp"))

    # ------------------------------------------------ calls
    def visit_Call(self, node):
        name = self.leaf(node.func)
        if name in ("getattr", "setattr", "hasattr", "vars", "import_module", "__import__"):
            target = node.args[0] if node.args else None
            computed = len(node.args) > 1 and not isinstance(node.args[1], ast.Constant)
            on_framework = target is not None and (self.framework(target) or _framework_import(target) or (
                isinstance(target, ast.Name) and target.id in DYNAMIC_MODULES.get(self.path, ())))
            if name in ("getattr", "setattr", "hasattr", "vars") and on_framework and (computed or name == "vars"):
                hit("PY-COMPUTED", self.path, node.lineno, self.snip(node))
            if name == "setattr" and len(node.args) == 3 and _key(node.args[1]):
                self._check_attr(_key(node.args[1]), node.args[2])
        for k in node.keywords:
            if k.arg == "do_escape" and not (isinstance(k.value, ast.Constant) and k.value.value is True):
                hit("PY-RAW", self.path, node.lineno, self.snip(node))
        if name in RESPONSES:
            self._check_response(node, name)
        if isinstance(node.func, ast.Attribute):
            self._check_method_write(node)
        if name in ("raw_html", "js") and node.args:
            tmpl = self._resolve(node.args[0])
            if not self._is_const(tmpl):
                hit("PY-RAW", self.path, node.lineno, self.snip(node))
            elif _key(tmpl) is not None:
                for pos in hole_positions(_key(tmpl), "html" if name == "raw_html" else "js"):
                    hit("PY-POSITION", self.path, node.lineno, f"{pos}: {self.snip(node)}")
        tag = self._tag_of(node, name)
        if tag is not None:
            self._check_tag(node, tag)
        self.generic_visit(node)

    def _tag_of(self, node, name):
        """The element a call builds: a capitalized builder, a lowercase builder's constant tag,
        a call on a name bound to an element, or .set(...) on any receiver; None otherwise."""
        if name in LOWER_BUILDERS and (self.framework(node.func) or not self.qual(node.func)):
            first = self._resolve(node.args[0]) if node.args else None
            if _key(first) is None:
                hit("PY-RAW", self.path, node.lineno, self.snip(node))
                return ""
            return _key(first).lower()
        if self._element_builder(node.func, name):
            return name.lower()
        bound = self._resolve(node.func) if isinstance(node.func, ast.Name) else None
        if isinstance(bound, ast.Call) and self._tag_of_quiet(bound):
            return self._tag_of_quiet(bound)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "set" and node.keywords:
            return "?"
        return None

    def _element_builder(self, func, name):
        """A capitalized callee from the framework or from this repo (components wrap elements);
        a capitalized class imported from any other package (Request, BaseModel) is not one."""
        if not name or not name[0].isupper() or name in RESPONSES:
            return False
        if isinstance(func, ast.Name) and func.id in self.classes:
            return False
        q = self.qual(func)
        if q and q.startswith(FRAMEWORK_PREFIXES):
            return True
        local = q is None or q.startswith(".") or q.split(".")[0] in LOCAL_TOPS
        return local and self.path.startswith(UI_ROOTS)

    def _tag_of_quiet(self, node):
        name = self.leaf(node.func)
        if self._element_builder(node.func, name):
            return name.lower()
        if name in LOWER_BUILDERS and node.args and _key(node.args[0]):
            return _key(node.args[0]).lower()
        return None

    def _check_tag(self, node, tag):
        lower = self.leaf(node.func) in LOWER_BUILDERS
        children = node.args[1:] if lower else node.args
        if tag == "script":
            for c in children:
                self._check_hole(c, "PY-JSHOLE", ENCODERS_JS)
        elif tag == "style":
            for c in children:
                self._check_css(c)
        mapping_params = self.mapping_params[-1] if self.mapping_params else set()
        for idx, c in enumerate(children):
            c_res = self._resolve(c)
            if isinstance(c_res, ast.Dict):
                self._check_attr_dict(c_res)
            elif (isinstance(c_res, ast.Call) and isinstance(c_res.func, ast.Name) and c_res.func.id in
                  ("bytes", "bytearray", "memoryview")) or (_call_name(c_res) == "encode" and not c_res.args):
                hit("PY-RAW", self.path, c.lineno, self.snip(c))
            elif idx > 0 and isinstance(c, (ast.Subscript, ast.Call)) and self._from_mapping_param(c, mapping_params):
                hit("PY-RAW", self.path, c.lineno, self.snip(c))
        if tag == "meta":
            kws = {k.arg: k.value for k in node.keywords if k.arg}
            if (_key(kws.get("http_equiv")) or "").lower() == "refresh" and "content" in kws:
                self._check_hole(kws["content"], "PY-URLHOLE", set())
        for kw in node.keywords:
            if kw.arg is None:
                value = self._resolve(kw.value)
                if isinstance(value, ast.Dict):
                    self._check_attr_dict(value)
                elif _call_name(value) == "dict" and not value.args:
                    for k in value.keywords:
                        self._check_attr(k.arg, k.value, tag)
            else:
                self._check_attr(kw.arg, kw.value, tag)

    def _from_mapping_param(self, node, params):
        """row["meta"] or row.get("meta") where row is a parameter declared as a mapping:
        the element type is unknown, and a Mapping child becomes attributes."""
        base = node.value if isinstance(node, ast.Subscript) else (
            node.func.value if _call_name(node) == "get" and isinstance(node.func, ast.Attribute) else None)
        return isinstance(base, ast.Name) and base.id in params

    def _check_attr_dict(self, node):
        for k, v in zip(node.keys, node.values):
            if k is None:  # {**other}
                continue
            key = _key(k)
            if key is None:
                hit("PY-RAW", self.path, k.lineno, self.snip(k))
            else:
                self._check_attr(key, v)

    def _check_attr(self, key, value, tag="?"):
        """One attribute position, wherever it is set (keyword, mapping, after construction)."""
        if not key or value is None:
            return
        k = key.lower()
        if k in URL_ATTR_TAG and tag not in (URL_ATTR_TAG[k], "?"):
            return
        if EVENT_ATTR_RE.match(k):
            self._check_hole(value, "PY-JSHOLE", ENCODERS_JS)
        elif k in URL_ATTRS:
            self._check_url(value)
        elif k == "srcdoc":
            if not self._is_const(value):
                hit("PY-RAW", self.path, value.lineno, self.snip(value))
        elif k == "style":
            self._check_css(value)
        elif k in HTMX_JS_ATTRS:
            self._check_htmx(k, value)

    def _check_htmx(self, k, value):
        value = self._resolve(value)
        if self._is_const(value) or self._json_value(value):
            return
        text = _const_text(value) or ""
        if "trigger" in k:
            inside = re.search(r"\[[^\]]*" + re.escape(HOLE), text)
            if inside or not isinstance(value, ast.JoinedStr):
                hit("PY-JSHOLE", self.path, value.lineno, self.snip(value))
        elif text.lstrip().startswith("js:") or not isinstance(value, ast.JoinedStr) or text.startswith(HOLE):
            hit("PY-JSHOLE", self.path, value.lineno, self.snip(value))

    def _json_value(self, node, depth=0):
        """json.dumps/hx_vals output, or an in-repo function whose every return is one (never "js:")."""
        node = self._resolve(node)
        if isinstance(node, ast.Dict):
            return True  # FastHTML serializes a mapping value as JSON (fastcore valmap)
        if isinstance(node, ast.IfExp):
            return all(self._is_const(b) or self._json_value(b, depth + 1) for b in (node.body, node.orelse))
        name = _call_name(node)
        if name in ENCODERS_JS | {"hx_vals", "dumps"}:
            return True
        fn = self.funcs.get(name) if isinstance(getattr(node, "func", None), ast.Name) else None
        if fn is None and name in self.imported:
            fn = MODULE_FUNCS.get(self.imported[name][0], {}).get(self.imported[name][1])
        if fn is None or depth > 4:
            return False
        rets = [r.value for r in ast.walk(fn) if isinstance(r, ast.Return)]
        return bool(rets) and all(r is not None and (self._is_const(r) or _call_name(r) in {"hx_vals", "dumps"})
                                  for r in rets)

    def _check_css(self, value):
        value = self._resolve(value)
        if value is None or self._is_const(value) or self._numeric(value):
            return
        if isinstance(value, ast.JoinedStr):
            for v in value.values:
                if isinstance(v, ast.FormattedValue) and not (self._is_const(v.value) or self._numeric(v.value)):
                    hit("PY-CSSHOLE", self.path, value.lineno, self.snip(v.value))
            return
        hit("PY-CSSHOLE", self.path, getattr(value, "lineno", 0), self.snip(value))

    def _numeric(self, node):
        node = self._resolve(node)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return True
        if isinstance(node, ast.BinOp):
            return self._numeric(node.left) and self._numeric(node.right)
        if isinstance(node, ast.UnaryOp):
            return self._numeric(node.operand)
        return _call_name(node) in NUMERIC_CALLS

    def _check_response(self, node, name):
        kws = {k.arg: k.value for k in node.keywords if k.arg}
        content = node.args[0] if node.args else kws.get("content", kws.get("path"))
        media = kws.get("media_type")
        if media is None and name != "FileResponse" and len(node.args) > 3:
            media = node.args[3]
        if media is not None:
            m = self._resolve(media)
            if _call_name(m) == "inert_media_type":
                pass
            elif not self._is_const(m) or ("html" in str(_const_text(m) or "") and not self._is_const(content)):
                hit("PY-HTMLRESP", self.path, node.lineno, self.snip(node))
        elif name == "FileResponse":
            path = node.args[0] if node.args else kws.get("path")
            if path is not None and not self._is_const(path):
                hit("PY-HTMLRESP", self.path, node.lineno, self.snip(node))
        headers = self._resolve(kws["headers"]) if "headers" in kws else None
        if isinstance(headers, ast.Dict):
            for k, v in zip(headers.keys, headers.values):
                key = _key(k) if k is not None else ""
                if key is None or (key.lower() == "content-type" and (
                        not self._is_const(v) or "html" in str(_const_text(self._resolve(v)) or ""))):
                    hit("PY-HTMLRESP", self.path, node.lineno, self.snip(node))

    def _check_method_write(self, node):
        f = node.func
        recv = f.value
        if f.attr == "init_headers":
            hit("PY-HTMLRESP", self.path, node.lineno, self.snip(node))
        elif isinstance(recv, ast.Attribute) and recv.attr == "headers" and f.attr in (
                "update", "setdefault", "__setitem__", "append"):
            for a in node.args:
                a = self._resolve(a)
                if isinstance(a, ast.Dict):
                    if any(k is None or _key(k) is None or _key(k).lower() == "content-type" for k in a.keys):
                        hit("PY-HTMLRESP", self.path, node.lineno, self.snip(node))
                elif _key(a) is None or _key(a).lower() == "content-type":
                    hit("PY-HTMLRESP", self.path, node.lineno, self.snip(node))
                    break
        elif isinstance(recv, ast.Attribute) and recv.attr == "attrs" and f.attr in ("update", "__setitem__", "setdefault"):
            for a in node.args:
                a = self._resolve(a)
                if isinstance(a, ast.Dict):
                    self._check_attr_dict(a)
                else:
                    hit("PY-RAW", self.path, node.lineno, self.snip(node))
                    break

    # ------------------------------------------------ folding
    def _resolve(self, node, depth=0):
        if not isinstance(node, ast.Name) or depth > 8:
            return node
        for scope in reversed(self.locals):
            if node.id in scope:
                return self._resolve(scope[node.id], depth + 1)
        if node.id in self.consts:
            return self._resolve(self.consts[node.id], depth + 1)
        if node.id in self.imported:
            mod, name = self.imported[node.id]
            target = MODULE_CONSTS.get(mod, {}).get(name)
            if target is not None:
                return target
        return node

    def _is_const(self, node, depth=0):
        node = self._resolve(node)
        if depth > 8:
            return False
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, ast.JoinedStr):
            return all(isinstance(v, ast.Constant) or self._is_const(v.value, depth + 1) for v in node.values)
        if isinstance(node, ast.BinOp):
            return self._is_const(node.left, depth + 1) and self._is_const(node.right, depth + 1)
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(self._is_const(e, depth + 1) for e in node.elts)
        if isinstance(node, ast.IfExp):
            return self._is_const(node.body, depth + 1) and self._is_const(node.orelse, depth + 1)
        if isinstance(node, ast.Call) and _call_name(node) in NUMERIC_CALLS:
            return True
        return False

    def _check_hole(self, value, code, encoders, depth=0, at=None):
        """Hit every non-constant, unencoded part of *value*; *at* is the use site when the
        value is a constant imported from another module (its nodes carry that module's lines)."""
        if at is None and isinstance(value, ast.Name) and value.id in self.imported \
                and value.id not in self.consts and not any(value.id in sc for sc in self.locals):
            at = value
        value = self._resolve(value)
        if value is None or self._is_const(value) or depth > 8:
            return
        if isinstance(value, ast.JoinedStr):
            for v in value.values:
                if isinstance(v, ast.FormattedValue) and not self._is_const(v.value) \
                        and _call_name(v.value) not in encoders:
                    hit(code, self.path, (at or value).lineno, self.snip(at or v.value))
            return
        if isinstance(value, (ast.BinOp, ast.IfExp)):
            for part in (value.left, value.right) if isinstance(value, ast.BinOp) else (value.body, value.orelse):
                self._check_hole(part, code, encoders, depth + 1, at)
            return
        if _call_name(value) in encoders:
            return
        hit(code, self.path, getattr(at or value, "lineno", 0), self.snip(at or value))

    def _lead(self, node, depth=0):
        """The literal text a URL expression starts with, when it is known."""
        node = self._resolve(node)
        if depth > 8:
            return None
        if isinstance(node, ast.Constant):
            return str(node.value)
        if isinstance(node, ast.JoinedStr) and node.values:
            head = node.values[0]
            return str(head.value) if isinstance(head, ast.Constant) else self._lead(head.value, depth + 1)
        if isinstance(node, ast.BinOp):
            return self._lead(node.left, depth + 1)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            fn = self.funcs.get(node.func.id)
            if fn is None and node.func.id in self.imported:
                fn = MODULE_FUNCS.get(self.imported[node.func.id][0], {}).get(self.imported[node.func.id][1])
            if fn is not None:
                leads = {self._lead(r.value, depth + 1) if r.value is not None else None
                         for r in ast.walk(fn) if isinstance(r, ast.Return)}
                safe = [ld for ld in leads if ld is not None and SAFE_PATH_RE.match(ld)]
                if leads and len(safe) == len(leads):
                    return safe[0]
        return None

    def _check_url(self, value):
        value = self._resolve(value)
        if value is None:
            return
        if isinstance(value, ast.IfExp):
            self._check_url(value.body)
            self._check_url(value.orelse)
            return
        lead = self._lead(value)
        if lead is not None and (SAFE_PATH_RE.match(lead) or (self._is_const(value) and not SCHEME_RE.search(lead))):
            return
        if _call_name(value) in ENCODERS_URL:
            return
        hit("PY-URLHOLE", self.path, getattr(value, "lineno", 0), self.snip(value))


# ---------------------------------------------------------------- driver
def in_scope(path: str) -> str | None:
    p = Path(path)
    parts = p.parts
    if path.endswith(".py"):
        if "tests" in parts or "migrations" in parts or p.name.startswith(("test_", "conftest")):
            return None
        return "py"
    if path.endswith(".js"):
        if path.endswith(".min.js") or "vendor" in parts or "tests" in parts:
            return None
        if path.startswith(("ui/static/", "electron/assets/")) or path == "electron/preload.js" \
                or "/static/" in path:
            return "js"
    return None


def main(argv):
    root = Path(argv[1])
    try:
        files = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True,
                               check=True).stdout.split()
    except subprocess.CalledProcessError as e:
        print(f"not a git checkout: {root}: {e.stderr.strip()}", file=sys.stderr)
        return 1
    trees = {}
    for f in files:
        if in_scope(f) == "py":
            try:
                trees[f] = ast.parse((root / f).read_text(encoding="utf-8", errors="replace"), f)
            except SyntaxError:
                continue
            LOCAL_TOPS.add(module_name(f).split(".")[0])
            MODULE_CONSTS[module_name(f)] = _inline_consts({
                st.targets[0].id: st.value for st in trees[f].body
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)})
            DYNAMIC_MODULES[f] = {
                tg.id for sub in ast.walk(trees[f]) if isinstance(sub, ast.Assign)
                and _framework_import(sub.value)
                for tg in sub.targets if isinstance(tg, ast.Name)}
            MODULE_FUNCS[module_name(f)] = {
                st.name: st for st in trees[f].body if isinstance(st, ast.FunctionDef)}
    for f in files:
        kind = in_scope(f)
        if not kind or f == ENCODER_PATH:
            continue
        src = (root / f).read_text(encoding="utf-8", errors="replace")
        if kind == "js":
            before = len(hits)
            scan_js(src, f, 1)
            if f == ADMISSION_JS:  # the browser half of the encoder module: its own writes are the door
                hits[before:] = [h for h in hits[before:] if h[0] not in ("JS-HTML", "JS-NAV", "JS-COMPUTED")]
            continue
        try:
            tree = ast.parse(src, f)
        except SyntaxError as e:
            hit("PARSE", f, e.lineno or 0, str(e))
            continue
        PyScan(f, src).visit(tree)
    seen = set()
    for code, path, line, snip in sorted(hits, key=lambda h: (h[1], h[2], h[0])):
        if (code, path, line, snip) in seen:
            continue
        seen.add((code, path, line, snip))
        print(f"{code} | {path}:{line} | {snip}")
    return len(seen)


if __name__ == "__main__":
    sys.exit(min(main(sys.argv), 255))
