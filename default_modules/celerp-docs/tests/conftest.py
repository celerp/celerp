"""Conftest for celerp-docs module tests - inherits root conftest fixtures."""
import pytest


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
