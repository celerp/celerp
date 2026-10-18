# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp_l10n_zz - a test-only country module for the made-up country ZZ.

It fills the fixed_asset_rules slot with a depreciation method, a timing convention,
an asset-by-asset tax regime, two pool regimes (one carrying deferred tax) and a
statutory form, plus two entries the fixed assets module must skip and name: one
missing what its kind needs, and one reusing a core key. Never shipped.
"""

PLUGIN_MANIFEST = {
    "name": "celerp_l10n_zz",
    "version": "0.1.0",
    "display_name": "ZZ localization",
    "description": "Fixed asset rules for country ZZ (test only).",
    "license": "MIT",
    "author": "Celerp",
    "depends_on": ["celerp-fixed-assets"],
    "slots": {
        "fixed_asset_rules": [
            {"kind": "method", "key": "zz_fixed_rate", "handler": "celerp_l10n_zz.rules:FixedRate"},
            {"kind": "convention", "key": "zz_quarter_start", "handler": "celerp_l10n_zz.rules:QuarterStart"},
            {"kind": "tax_regime", "key": "zz_asset", "handler": "celerp_l10n_zz.rules:AssetRegime"},
            {"kind": "tax_regime", "key": "zz_pool", "handler": "celerp_l10n_zz.rules:PoolRegime"},
            {"kind": "tax_regime", "key": "zz_pool_plain", "handler": "celerp_l10n_zz.rules:PlainPoolRegime"},
            {"kind": "statutory_report", "key": "zz_form_a", "handler": "celerp_l10n_zz.rules:FormA"},
            {"kind": "method", "key": "zz_broken", "handler": "celerp_l10n_zz.rules:Broken"},
            {"kind": "method", "key": "straight_line", "handler": "celerp_l10n_zz.rules:Shadow"},
        ],
    },
    "locales": {"en": {"file": "locales/en.json"}},
}
