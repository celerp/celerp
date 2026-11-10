# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The JSON text Celerp stores, from every engine and data migration alike."""
from __future__ import annotations

import json


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
