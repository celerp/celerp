# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Test fixture module: exercises the i18n `locales` manifest seam.

Declares one new language ("xx") and one right-to-left language ("xr") so the
loader push path (manifest -> register_catalog -> _load) can be tested end to
end. Strings are ASCII test placeholders only, never shipped copy.
"""

PLUGIN_MANIFEST = {
    "name": "celerp-testlang",
    "version": "1.0.0",
    "display_name": "Test Language Module",
    "locales": {
        "xx": {"file": "locales/xx.json", "rtl": False},
        "xr": {"file": "locales/xr.json", "rtl": True},
    },
}
