# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from __future__ import annotations

import re
from datetime import datetime, UTC

from celerp.models.company import Company

_PREFIX_BY_DOC_TYPE = {
    "invoice": "INV",
    "proforma": "PF",
    "purchase_order": "PO",
    "quotation": "QUO",
    "shipping_doc": "SHIP",
    "credit_note": "CN",
    "bill": "BILL",
    "memo": "MEMO",
    "list": "LST",
    "consignment_in": "CI",
}

# Default pattern for all new companies
DEFAULT_PATTERN = "{PREFIX}-{YY}{MM}-{####}"


def _expand_pattern(pattern: str, prefix: str, now: datetime, seq_num: int) -> str:
    """Expand a numbering pattern into a concrete document reference."""
    # Count # chars to determine padding width
    match = re.search(r"\{(#+)\}", pattern)
    pad = len(match.group(1)) if match else 2

    result = pattern
    result = result.replace("{PREFIX}", prefix)
    result = result.replace("{YYYY}", str(now.year))
    result = result.replace("{YY}", f"{now.year % 100:02d}")
    result = result.replace("{MM}", f"{now.month:02d}")
    result = result.replace("{DD}", f"{now.day:02d}")
    # Replace the {##...} token with zero-padded number
    result = re.sub(r"\{#+\}", f"{seq_num:0{pad}d}", result)
    return result


def _should_reset(pattern: str, seq: dict, now: datetime) -> bool:
    """Determine if the sequence counter should reset based on the date tokens in the pattern."""
    stored_year = int(seq.get("year", 0))
    stored_month = int(seq.get("month", 0))

    if stored_year != now.year:
        return True
    if "{MM}" in pattern and stored_month != now.month:
        return True
    if "{DD}" in pattern and int(seq.get("day", 0)) != now.day:
        return True
    return False


def validate_pattern(pattern: str) -> str | None:
    """Return error message if pattern is invalid, None if OK."""
    if not re.search(r"\{#+\}", pattern):
        return "Pattern must contain a sequence token like {##} or {####}"
    # Check for unknown tokens
    cleaned = re.sub(r"\{(PREFIX|YYYY|YY|MM|DD|#+)\}", "", pattern)
    unknown = re.findall(r"\{[^}]+\}", cleaned)
    if unknown:
        return f"Unknown tokens: {', '.join(unknown)}"
    return None


def preview_pattern(pattern: str, prefix: str, seq_num: int = 1) -> str:
    """Generate a preview of what the pattern produces right now."""
    return _expand_pattern(pattern, prefix, datetime.now(UTC), seq_num)


def next_doc_ref(company: Company, doc_type: str) -> str:
    """Generate the next document reference and increment the sequence counter."""
    if doc_type not in _PREFIX_BY_DOC_TYPE:
        raise ValueError(f"Unsupported doc_type for sequence: {doc_type}")

    now = datetime.now(UTC)
    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    seq = dict(sequences.get(doc_type) or {})

    prefix = str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[doc_type])
    pattern = str(seq.get("pattern") or DEFAULT_PATTERN)

    if _should_reset(pattern, seq, now):
        seq["next"] = 1

    next_num = int(seq.get("next", 1))
    ref = _expand_pattern(pattern, prefix, now, next_num)

    # Update sequence state
    seq["next"] = next_num + 1
    seq["year"] = now.year
    seq["month"] = now.month
    seq["day"] = now.day
    seq["prefix"] = prefix
    seq["pattern"] = pattern
    sequences[doc_type] = seq
    settings["sequences"] = sequences
    company.settings = settings

    return ref


def get_all_sequences(company: Company) -> list[dict]:
    """Return the numbering config for all doc types."""
    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    now = datetime.now(UTC)
    result = []
    for doc_type, default_prefix in _PREFIX_BY_DOC_TYPE.items():
        seq = dict(sequences.get(doc_type) or {})
        prefix = str(seq.get("prefix") or default_prefix)
        pattern = str(seq.get("pattern") or DEFAULT_PATTERN)
        next_num = int(seq.get("next", 1))
        result.append({
            "doc_type": doc_type,
            "prefix": prefix,
            "pattern": pattern,
            "next": next_num,
            "preview": _expand_pattern(pattern, prefix, now, next_num),
        })
    return result


def update_sequence(company: Company, doc_type: str, prefix: str | None = None,
                    pattern: str | None = None, next_num: int | None = None) -> dict:
    """Update numbering config for a single doc type. Returns the updated sequence."""
    if doc_type not in _PREFIX_BY_DOC_TYPE:
        raise ValueError(f"Unsupported doc_type: {doc_type}")

    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    seq = dict(sequences.get(doc_type) or {})

    if prefix is not None:
        seq["prefix"] = prefix
    if pattern is not None:
        err = validate_pattern(pattern)
        if err:
            raise ValueError(err)
        seq["pattern"] = pattern
    if next_num is not None:
        seq["next"] = max(1, next_num)

    sequences[doc_type] = seq
    settings["sequences"] = sequences
    company.settings = settings

    now = datetime.now(UTC)
    pfx = str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[doc_type])
    pat = str(seq.get("pattern") or DEFAULT_PATTERN)
    n = int(seq.get("next", 1))
    return {
        "doc_type": doc_type,
        "prefix": pfx,
        "pattern": pat,
        "next": n,
        "preview": _expand_pattern(pat, pfx, now, n),
    }
