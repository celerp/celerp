import pytest

from celerp.db_url import sync_url


@pytest.mark.parametrize("url", [
    "postgresql://u:p@localhost:5432/celerp",
    "postgresql+asyncpg://u:p@localhost:5432/celerp",
    "postgresql+psycopg2://u:p@localhost:5432/celerp",
])
def test_sync_url_names_the_shipped_driver(url):
    assert sync_url(url) == "postgresql+psycopg2://u:p@localhost:5432/celerp"


def test_sync_url_keeps_query_parameters():
    url = "postgresql+asyncpg://postgres@/celerp?host=/tmp/pg"
    assert sync_url(url) == "postgresql+psycopg2://postgres@/celerp?host=/tmp/pg"


def test_sync_url_leaves_other_databases_alone():
    assert sync_url("sqlite:///celerp.db") == "sqlite:///celerp.db"
