# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Request-capacity constants shared by the API pool and the local UI transport.

The app launches one uvicorn worker (both `celerp start` and the Electron shell
run `uvicorn celerp.main:app` without `--workers`), so one API process owns one
request connection pool. These constants are the single source of truth for that
pool's size and for how many connections the web UI is allowed to open into that
process, so the two can never drift apart. The base size is the interactive
ceiling; the overflow is held back for background work, relay, auth, and bursts.

This module has no side effects and imports nothing from the app, so both the
database engine and the UI transport can read it without an import cycle.
"""
from __future__ import annotations

# Base connections the single API request pool keeps open, and the extra it may
# open under burst before refusing. The web UI's interactive transport uses the
# base size as its own connection ceiling, so a page fan-out can never demand
# more connections than the pool holds for interactive work.
REQUEST_DB_POOL_SIZE = 10
REQUEST_DB_MAX_OVERFLOW = 5
