# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Synthetic Manager business files for tests and the shipped sample.

Provenance: `encoder.py` writes every file here from the specs in
`specs.py`. The encoder is written independently of the adapter's reader: it
has its own protobuf wire writer and its own SQLite writer, so a shared
misunderstanding cannot make both sides agree by accident. The file layout
(the Objects table, content-type GUIDs, field numbers and the protobuf-net
encodings of Guid, decimal and DateTime) is interoperability-derived from
observing the format. No vendor source code is copied, and no vendor sample
file is included or used. All business names, people and figures are
invented. The expected figures in `checkpoints.json` are worked out by hand
from the specs, not produced by the adapter.
"""
