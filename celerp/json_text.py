# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The JSON text Celerp stores, from every engine and data migration alike."""
from __future__ import annotations

import json
import re


def dump_json(value) -> str:
    """``value`` as JSON text with every character past ASCII written as itself (UTF-8), not
    as a \\u escape: a SQL_ASCII database cannot read such an escape back (->> refuses it), so
    an engine or migration that wrote one would leave rows the app cannot query. Control
    characters stay escaped, as JSON requires."""
    return json.dumps(value, ensure_ascii=False)


# How every psycopg2 engine sends and reads text: JSON as dump_json writes it, over a UTF-8
# connection whatever the server encoding. A SQL_ASCII database keeps the bytes it is sent, and a
# client in the server's own encoding could neither send that JSON nor read those bytes back.
SYNC_TEXT = {"json_serializer": dump_json, "client_encoding": "utf8"}

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def dump_existing_json(value) -> str:
    """``value`` of a row that already exists, as dump_json writes it, except that a lone
    surrogate (an escape such as \\ud83d without its partner, which json.loads keeps as is)
    is written back as its \\u escape: UTF-8 cannot encode it, so dump_json's text would stop
    the migration or restore that rewrites the row. json.loads joins every valid pair, so only
    lone halves are left in that range; a value without one comes out as dump_json writes it."""
    return _LONE_SURROGATE.sub(lambda m: "\\u%04x" % ord(m.group()), dump_json(value))


# The text settings of an engine that runs migration code: SYNC_TEXT, writing JSON through
# dump_existing_json, since a migration rewrites rows earlier versions stored.
MIGRATION_TEXT = {**SYNC_TEXT, "json_serializer": dump_existing_json}
