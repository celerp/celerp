# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Each xdist worker starts on an empty database of its own (conftest_support).

The worker database (<base>_gwN) outlives the run that made it. A run that stops
before its teardown (a timeout, an interrupt, a test that committed and never
cleaned up) leaves rows behind, and the next run on the same name inherits them:
one leftover User makes every later first-user registration on that worker answer
"System already bootstrapped", and a leftover table keeps an old column layout
that create_all never changes. provision_worker_db therefore recreates the worker
database at the start of every run.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import psycopg2

from conftest_support import provision_worker_db

_PROBE_WORKER = "isoprobe"


def _connect(url: str, dbname: str | None = None):
    parts = urlsplit(url.replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=dbname or parts.path.lstrip("/"))
    conn.autocommit = True
    return conn


def _drop(base_url: str, url: str) -> None:
    conn = _connect(base_url)
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{urlsplit(url).path.lstrip("/")}"')
    finally:
        conn.close()


def test_a_worker_database_left_by_an_earlier_run_starts_empty():
    base_url = os.environ["DATABASE_URL"]
    url = provision_worker_db(base_url, _PROBE_WORKER)
    try:
        conn = _connect(url)
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS users (email text)")
                cur.execute("INSERT INTO users VALUES ('left-behind@example.com')")
        finally:
            conn.close()

        again = provision_worker_db(base_url, _PROBE_WORKER)

        assert again == url
        conn = _connect(again)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.users')")
                assert cur.fetchone()[0] is None, "the earlier run's users table survived"
        finally:
            conn.close()
    finally:
        _drop(base_url, url)


def test_the_base_database_named_by_the_caller_is_never_recreated():
    base_url = os.environ["DATABASE_URL"]
    conn = _connect(base_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_database WHERE datname = current_database()")
            before = cur.fetchone()[0]
    finally:
        conn.close()
    url = provision_worker_db(base_url, _PROBE_WORKER)
    try:
        assert url != base_url
        conn = _connect(base_url)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT oid FROM pg_database WHERE datname = current_database()")
                assert cur.fetchone()[0] == before
        finally:
            conn.close()
    finally:
        _drop(base_url, url)
