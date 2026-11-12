# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

PLUGIN_MANIFEST = {
    "name": "celerp-subscriptions",
    "version": "2.0.0",
    "display_name": "Subscriptions",
    "description": "Recurring invoice and purchase order agreements. Each period is billed once, on its bill date, by hand or automatically.",
    "license": "MIT",
    "author": "Celerp",
    "api_routes": "celerp_subscriptions.routes",
    "ui_routes": "celerp_subscriptions.ui_routes",
    "depends_on": ["celerp-docs"],
    "slots": {
        "nav": [
            {"group": "Subscriptions", "key": "subscriptions_sales", "href": "/subscriptions?direction=sales", "label": "Sales Subscriptions", "label_key": "nav.subscriptions_sales", "order": 25, "permission": "view_subscriptions"},
            {"group": "Subscriptions", "key": "subscriptions_purchasing", "href": "/subscriptions?direction=purchasing", "label": "Purchasing Subscriptions", "label_key": "nav.subscriptions_purchasing", "order": 26, "permission": "view_subscriptions"},
            {"group": "Subscriptions", "key": "retainers", "href": "/retainers", "label": "Retainers", "label_key": "nav.retainers", "order": 27, "permission": "view_subscriptions", "verticals": ["consulting"]},
            {"group": "Subscriptions", "key": "members", "href": "/members", "label": "Members", "label_key": "nav.members", "order": 28, "permission": "view_subscriptions", "verticals": ["nonprofit"]},
        ],
        "on_modules_ready": [{"handler": "celerp_subscriptions.migration:migrate_schedules_hook"}],
        "demo_records": {"handler": "celerp_subscriptions.demo:seed_demo_agreements_hook"},
        "on_company_restored": [{"handler": "celerp_subscriptions.migration:reconcile_restored_company_schedules"}],
        "on_company_module_enabled": [{"handler": "celerp_subscriptions.migration:reconcile_company_schedules"}],
        "periodic_operation": {"handler": "celerp_subscriptions.billing:run_due", "module": "celerp-subscriptions"},
        "doc_status_guard": [{"handler": "celerp_subscriptions.billing:guard_bill_status"},
                             {"handler": "celerp_subscriptions.billing:guard_agreement_status"}],
        # A document an agreement made leads back to it and names the period it bills.
        "doc_detail_badges": [{"render": "celerp_subscriptions.ui_routes:doc_period_badge",
                               "permission": "view_subscriptions"}],
        "search_provider": {
            "handler": "celerp_subscriptions.search:global_search",
            "result_key": "items",
            "permission": "view_subscriptions",
        },
    },
    "migrations": None,
    "help": {"dir": "celerp_subscriptions/help"},
}
