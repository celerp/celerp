# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Number types shared by request bodies."""
from __future__ import annotations

from typing import Annotated

from pydantic import Field

# An amount, price, rate or quantity sent in a request. JSON allows NaN and Infinity,
# which are never a real figure, so they are refused as invalid input.
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]

# A quantity that must be more than nothing, such as how much of a component a run uses.
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
