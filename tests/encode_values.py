# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Hostile values for the encoder's generative tests.

Each value is built from fragments that end or reopen a script element, an HTML comment, a
JavaScript string, comment or line, an attribute value or a tag, mixed with arbitrary
characters, so a test that holds over many seeded draws holds for the characters a browser
treats specially, not only for a hand-written list.
"""
from __future__ import annotations

import random

PWN = "window.__pwn=1"
FRAGMENTS = [
    '"', "'", "`", "\\", "\\'", '\\"', "<", ">", "&", "/", "*", "\r", "\n", "\r\n", " ", " ",
    "\t", "\f", "\x00", " ", "=", "(", ")", ";", ",", "{", "}", "[", "]", "${", "//", "/*", "*/",
    "</script", "</SCRIPT >", "<script>", "<!--", "-->", "--!>", "<!-->", "]]>", "</style>",
    "</textarea>", "</title>", "&quot;", "&#39;", "&lt;", "&amp;", "\\u003c", "\\x3c",
    f'");{PWN};//', f"');{PWN};//", f"`;{PWN};//", f"*/{PWN};/*", f"\n{PWN}//", f" {PWN}//",
    f"</script><script>{PWN}</script>", f'"><img src=x onerror={PWN}>', f"' onmouseover={PWN} '",
    f"javascript:{PWN}", "this", "event", "null", "undefined", "__proto__",
]


def hostile_string(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(0, 8)):
        if rng.random() < 0.75:
            parts.append(rng.choice(FRAGMENTS))
        else:
            parts.append(chr(rng.choice([rng.randint(0x20, 0x7e), rng.randint(0xa0, 0x2fff),
                                         rng.randint(0x1f600, 0x1f64f)])))
    return "".join(parts)


def hostile_values(rng: random.Random) -> list:
    """One draw of values of every JSON kind, strings built from ``FRAGMENTS``."""
    s = hostile_string(rng)
    return [s, s, s, rng.randint(-10**12, 10**12), rng.random() * 1e6, True, False, None,
            [s, hostile_string(rng), 1], {s: hostile_string(rng), "k": [None, s]}]
