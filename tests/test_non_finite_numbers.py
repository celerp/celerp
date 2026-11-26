# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A number that is not finite (inf, nan) never reaches the API as a request body: the local
client refuses it as invalid input, so a route that parsed one shows "invalid input" instead of
answering 500, and the labels editor reads it as unreadable, like any other bad text."""
from __future__ import annotations

import math

import pytest

from ui import api_client as api
from ui.api_client import APIError
from ui.i18n import t

_NON_FINITE = (math.inf, -math.inf, math.nan)


@pytest.mark.parametrize("value", _NON_FINITE, ids=["inf", "-inf", "nan"])
async def test_local_client_refuses_non_finite_numbers(value):
    async with api._local_client("tok") as client:
        with pytest.raises(APIError) as caught:
            client.build_request("PATCH", "/docs/doc:1", json={"line_items": [{"quantity": value}]})
        assert caught.value.status == 422
        assert api.error_message(caught.value) == t("error.invalid_input")
        # A finite number and a body that is not JSON still build as before.
        assert client.build_request("PATCH", "/docs/doc:1", json={"quantity": 1.5}).method == "PATCH"
        assert client.build_request("POST", "/x", content=b"inf").method == "POST"
    # Every API call that goes through the shared client context managers refuses it too,
    # before anything is sent.
    with pytest.raises(APIError) as caught:
        await api.create_doc("tok", {"doc_type": "invoice", "line_items": [{"quantity": value}]})
    assert caught.value.status == 422


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "Infinity", "1e309"])
def test_label_numbers_refuse_non_finite(raw):
    from celerp_labels.service import LabelNumberError, label_number
    with pytest.raises(LabelNumberError):
        label_number("width_mm", raw)
    assert label_number("width_mm", "25.5") == 25.5


@pytest.mark.parametrize("raw", ["1e309", "inf", "nan"])
async def test_writeoff_qty_rejects_non_finite(raw):
    """The write-off quantity cell answers "Enter a number" in place, keeping what was typed."""
    import html
    from unittest.mock import AsyncMock, patch

    from httpx import ASGITransport, AsyncClient

    from test_helpers import authed_cookies
    from ui.app import app as ui_app
    save = AsyncMock(return_value={})
    with patch("ui.api_client.set_writeoff_line", new=save), \
         patch("ui.api_client.get_list", new=AsyncMock(return_value={"id": "list:W1", "line_items": []})):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
            ui.cookies.update(authed_cookies("owner"))
            r = await ui.patch("/lists/list:W1/writeoff-line/L1/qty_out", data={"value": raw},
                               headers={"HX-Request": "true"})
    assert t("documents.enter_a_number") in html.unescape(r.text)
    assert f'value="{raw}"' in r.text
    save.assert_not_called()


def _bare_floats(model) -> list[str]:
    """The fields of ``model`` (its own, not inherited) holding a float that may be inf or nan."""
    import typing

    def bare(annotation, metadata=()) -> bool:
        if typing.get_origin(annotation) is typing.Annotated:
            inner, *more = typing.get_args(annotation)
            return bare(inner, (*more, *metadata))
        if annotation is float:
            flat = [m for meta in metadata for m in (meta, *getattr(meta, "metadata", ()))]
            return not any(getattr(m, "allow_inf_nan", None) is False for m in flat)
        return any(bare(arg) for arg in typing.get_args(annotation))

    model.model_rebuild()  # an annotation that does not resolve fails here instead of reading as no float
    own = model.__dict__.get("__annotations__", {})
    if model.model_config.get("allow_inf_nan") is False:
        return []
    return [name for name, field in model.model_fields.items()
            if name in own and bare(field.annotation, tuple(field.metadata))]


def _first_party_models():
    import importlib
    import pkgutil
    from pathlib import Path

    from pydantic import BaseModel

    root = Path(__file__).resolve().parents[1]
    packages = ["celerp"] + sorted(p.name for d in (root / "default_modules").iterdir() if d.is_dir()
                                   for p in d.iterdir() if (p / "__init__.py").is_file() and p.name != "tests")
    for package in packages:
        for info in pkgutil.walk_packages(importlib.import_module(package).__path__, package + "."):
            if not {"tests", "migrations", "alembic"} & set(info.name.split(".")):
                importlib.import_module(info.name)

    def subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)
    return {m for m in subclasses(BaseModel) if m.__module__.split(".")[0] in packages}


def test_every_float_a_schema_reads_is_finite():
    """A request body or event payload reads its numbers as FiniteFloat, so NaN, Infinity or a
    number past float range (1e400 reads as inf) is refused as invalid input, never a 500."""
    bare = sorted(f"{m.__module__}.{m.__qualname__}.{name}" for m in _first_party_models() for name in _bare_floats(m))
    assert not bare, f"fields reading a float that may be inf or nan, use FiniteFloat: {bare}"


def test_the_float_walk_sees_a_bare_float():
    from pydantic import BaseModel, Field

    from celerp.schemas.numbers import FiniteFloat

    class Line(BaseModel):
        amount: float = 0
        share: dict[str, list[float | None]] = Field(default_factory=dict)
        fine: FiniteFloat | None = None
        text: str = ""

    assert _bare_floats(Line) == ["amount", "share"]


def test_the_float_walk_refuses_a_size_it_cannot_resolve():
    from pydantic import BaseModel
    from pydantic.errors import PydanticUndefinedAnnotation

    class Label(BaseModel):
        width_mm: "Unresolved | None" = None  # noqa: F821

    with pytest.raises(PydanticUndefinedAnnotation):
        _bare_floats(Label)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_a_recipe_overhead_amount_must_be_finite(value):
    from pydantic import ValidationError

    from celerp.events.schemas import OverheadLine, RecipeSpec

    with pytest.raises(ValidationError):
        RecipeSpec(overhead=[{"description": "x", "amount": value}])
    assert OverheadLine(description="x", amount=12.5).amount == 12.5
