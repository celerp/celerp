# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Parameter descriptions shared by the served and exported OpenAPI schemas."""

import json
from functools import lru_cache
from pathlib import Path

_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")


@lru_cache(maxsize=1)
def _catalog() -> dict:
    path = Path(__file__).with_name("openapi_parameter_descriptions.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _parameters(schema: dict):
    for path, item in schema.get("paths", {}).items():
        for method in _METHODS:
            operation = item.get(method)
            if not isinstance(operation, dict):
                continue
            key = f"{method.upper()} {path}"
            for param in (item.get("parameters") or []) + (operation.get("parameters") or []):
                if isinstance(param, dict):
                    yield key, param


def describe_parameters(schema: dict) -> dict:
    """Fill only absent descriptions; explicit route documentation takes precedence."""
    catalog = _catalog()
    for operation, param in _parameters(schema):
        if isinstance(param.get("description"), str) and param["description"].strip():
            continue
        text = catalog.get(operation, {}).get(f"{param.get('in')}:{param.get('name')}")
        if isinstance(text, str) and text.strip():
            param["description"] = text
    return schema


def missing_parameter_descriptions(schema: dict) -> list[str]:
    """Return every parameter still lacking meaningful documentation."""
    return sorted(
        f"{operation} {param.get('in')}:{param.get('name')}"
        for operation, param in _parameters(schema)
        if not isinstance(param.get("description"), str) or not param["description"].strip()
    )
