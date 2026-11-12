# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Templates and values the encoder's position rule is proven against.

Every raw_html and js template in the repository, collected with the sink check's own
resolver, plus template shapes generated from small alphabets and kept when the encoder
admits them. Rendering any of them with a hostile value must give the same structure as
rendering it with a neutral one.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import random
import string
from html.parser import HTMLParser
from pathlib import Path

from ui.components.encode import hole_positions, js, raw_html

ROOT = Path(__file__).resolve().parents[1]
NEUTRAL = "a"
PWN = "window.__pwn=1"
HOSTILE = [
    '"', "'", "<", ">", "/", "`", "\n", " ", "</script>", '");x=1;//', "/;x=1;//", "${x}",
    f'");{PWN};//', f"');{PWN};//", f"/;{PWN};//", f"${{{PWN}}}", f"*/{PWN};/*", f" {PWN}",
    f"</script><script>{PWN}</script>", f'"><img src=x onerror={PWN}>', f"'><svg onload={PWN}>",
    "<!--", "-->", "]]>", '");}}}}]]]))@@',
]


# Holes the allow list refuses: a tag or attribute name, an unknown "<" opener, a regular expression
# or division, a template literal, an unusual attribute boundary and an unterminated quote or tag.
REFUSED_SHAPES = [
    ("html", "<{tag}>x</{tag}>"),
    ("html", "<{t} x>"),
    ("html", "</{t}>"),
    ("html", "<a{x}>"),
    ("html", '<a title="t"{x}>'),
    ("html", '<a title="t" {x}="1">'),
    ("html", "<p>a < {v}</p>"),
    ("html", "<p title=\"{v}\" / >"),
    ("html", '<p title="{v}>'),
    ("html", "<p title='{v}>"),
    ("html", "<p title=\"x\"{v}"),
    ("html", "<p {v}"),
    ("html", "<p>{v}<!-- c"),
    ("html", "<textarea>{v}</textarea>"),
    ("html", "<title>{v}</title>"),
    ("html", "<svg><style>{v}</style></svg>"),
    ("html", '<p data-hx-get="{v}">'),
    ("html", '<meta content="{v}">'),
    ("html", "<p>{v}</b x>"),
    ("html", "<p>{v}</p"),
    ("html", '<p title="a"class="{v}">'),
    ("html", "<p title={v}>"),
    ("html", "<p title=a{v}>"),
    ("js", "var r = /{0}/;"),
    ("js", "x/{0}"),
    ("js", "{0}/2"),
    ("js", "var s = `{0}`;"),
    ("js", "var s = `a`; f({0});"),
    ("js", "f(a + {0});"),
    ("js", "f({0} + a);"),
    ("js", "f({0}.x);"),
    ("js", "f(!{0});"),
    ("js", "f('{0}');"),
    ("js", 'f("a);f({0});'),
    ("js", "f({0}); // tail"),
    ("js", "f({0}); <!-- c"),
]


def repo_templates() -> list[tuple[str, str]]:
    """(kind, template) for every constant raw_html and js template the sink check reads."""
    spec = importlib.util.spec_from_file_location("_sink_check_templates", ROOT / "scripts" / "sink_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    seen: dict[tuple[str, str], None] = {}
    check = mod.hole_positions

    def record(template, kind):
        seen[(kind, template)] = None
        return check(template, kind)

    mod.hole_positions = record
    with contextlib.redirect_stdout(io.StringIO()):
        mod.main(["sink_check", str(ROOT)])
    return list(seen)


_HTML_ALPHABET = [
    "<p>", "</p>", '<div class="', '<a title="', "<span data-x='", "<b>", "</b>", "<br>", "<td>",
    "<svg>", "</svg>", "<math>", "<option>", '<img alt="', '<input value="', ' id="', " alt=", "=",
    '"', "'", ">", "<", "/", "`", "&amp;", "<!-- c -->", "<!--", "-->", "\n", " ", "txt", "{v}", "{w}",
]
_JS_ALPHABET = [
    "f(", "(", ")", ",", "=", ":", "[", "]", "?", "return ", "var b", "a", "x.y", "1", "!", "-", "+",
    ".", ";", "{{", "}}", "'s'", '"s"', "'", '"', "/* c */", "// c\n", "/", "`", " ", "\n", "{0}", "{1}",
]


def generated_templates(kind: str, n: int = 20000, seed: int = 20261112) -> list[str]:
    """Distinct random templates over the kind's alphabet that the encoder admits and that hold a hole."""
    rng = random.Random(seed)
    alphabet = _HTML_ALPHABET if kind == "html" else _JS_ALPHABET
    hole = "{v}" if kind == "html" else "{0}"
    out = set()
    for _ in range(n):
        template = "".join(rng.choice(alphabet) for _ in range(rng.randint(2, 9)))
        if hole in template and not hole_positions(template, kind):
            out.add(template)
    return sorted(out)


def fields(template: str) -> list[str]:
    return [f for _l, f, _s, _c in string.Formatter().parse(template) if f is not None]


def render(kind: str, template: str, value) -> str:
    """The template with every hole filled with *value*, as the encoder writes it."""
    if kind == "html":
        return str(raw_html(template, **{f: value for f in fields(template)}))
    names = fields(template)
    count = len(names) if all(f == "" for f in names) else max(int(f) for f in names) + 1
    return str(js(template, *[value] * count))


class _Shape(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.events: list[tuple] = []

    def handle_starttag(self, tag, attrs):
        self.events.append(("start", tag, tuple(name for name, _v in attrs)))

    def handle_startendtag(self, tag, attrs):
        self.events.append(("startend", tag, tuple(name for name, _v in attrs)))

    def handle_endtag(self, tag):
        self.events.append(("end", tag))

    def handle_comment(self, data):
        self.events.append(("comment",))


def shape(kind: str, rendered: str) -> list[tuple]:
    """The element and attribute structure html.parser reads (a script inside its element)."""
    parser = _Shape()
    parser.feed(f"<script>{rendered}</script>" if kind == "js" else rendered)
    parser.close()
    return parser.events
