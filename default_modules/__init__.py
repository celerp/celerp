# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
"""Celerp default bundled modules package.

This package exists so setuptools includes the default_modules directory
in the installed wheel. Modules are loaded dynamically by the module loader,
not via regular Python imports from this namespace.
"""
