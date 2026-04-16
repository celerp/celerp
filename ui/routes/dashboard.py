# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.config import get_token as _token, get_role as _get_role
from ui.components.table import fmt_money as _fmt_money
from ui.i18n import t, get_lang




# ---------------------------------------------------------------------------
# Vertical configuration
# ---------------------------------------------------------------------------
# Each entry defines what a business in that vertical cares about.
# Keys:
#   kpis:         list of KPI card specs (order = display order)
#   quick_links:  list of (href, label, description) tuples
#   charts:       list of chart ids to show ("ar_aging", "inventory_cat")
#   show_activity: bool
#
# KPI spec keys:
#   key:      unique id (used for CSS / data extraction)
#   label:    card header
#   value_fn: name of a function in _KPI_VALUES that computes the value string
#   sub_fn:   name of a function for the sub-label (optional)
#   href:     where the card links (optional)
#   alert_fn: name of a function returning bool - if True, card gets alert styling
# ---------------------------------------------------------------------------

_VERTICAL_CONFIGS: dict[str, dict] = {
    "gemstones": {
        "kpis": [
            {"key": "memo_out",       "label": "Memo Out",         "value_fn": "memo_balance",      "sub_fn": "memo_count_sub",        "href": "/inventory?filter=on_memo",                        "alert_fn": "memo_exposure_high"},
            {"key": "stock_value",    "label": "Stock Value",      "value_fn": "retail_total",      "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "cost_basis",     "label": "Cost Basis",       "value_fn": "cost_total",        "sub_fn": "margin_pct_sub",        "href": "/inventory", "min_role": "manager"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub", "href": "/docs?type=invoice&status=outstanding",         "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "past_due_sub",          "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order&status=pending"},
        ],
        "secondary_kpis": [
            {"key": "pipeline",       "label": "Deal Pipeline",    "value_fn": "deal_value_pipeline","sub_fn": "active_deals_sub",     "href": "/crm"},
            {"key": "reserved",       "label": "Items Reserved",   "value_fn": "items_reserved",    "sub_fn": None,                    "href": "/inventory?filter=reserved"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": None,                    "href": "/reports"},
        ],
        "quick_links": [
            ("/inventory",              "Stock",          "All inventory"),
            ("/inventory?filter=on_memo","Memo Out",       "Items on consignment"),
            ("/docs?type=invoice",       "Invoices",       "Sales invoices"),
            ("/docs?type=purchase_order","Purchase Orders","Supplier orders"),
            ("/crm",                     "Contacts",       "CRM"),
            ("/reports/ar-aging",        "AR Aging",       "Outstanding receivables"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "coins_precious_metals": {
        "kpis": [
            {"key": "stock_cost",     "label": "Stock Value (Cost)","value_fn": "cost_total",       "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "stock_retail",   "label": "Stock Value (Retail)","value_fn": "retail_total",  "sub_fn": "margin_pct_sub",        "href": "/inventory", "min_role": "operator"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",   "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "memo_out",       "label": "Memo Out",         "value_fn": "memo_balance",     "sub_fn": "memo_count_sub",        "href": "/inventory?filter=on_memo"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count","sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order&status=pending"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",      "sub_fn": None,                    "href": "/reports"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",               "Stock",           "All stock"),
            ("/docs?type=invoice",        "Sales",           "Sales invoices"),
            ("/docs?type=purchase_order", "Purchases",       "Supplier orders"),
            ("/crm",                      "Contacts",        "CRM"),
            ("/reports/ar-aging",         "AR Aging",        "Outstanding receivables"),
            ("/inventory?filter=on_memo", "Memo Out",        "On consignment"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "watches": {
        "kpis": [
            {"key": "stock_value",    "label": "Stock Value",      "value_fn": "retail_total",      "sub_fn": "active_pieces_sub",     "href": "/inventory"},
            {"key": "cost_basis",     "label": "Cost Basis",       "value_fn": "cost_total",        "sub_fn": "margin_pct_sub",        "href": "/inventory", "min_role": "manager"},
            {"key": "memo_out",       "label": "Memo Out",         "value_fn": "memo_balance",      "sub_fn": "memo_count_sub",        "href": "/inventory?filter=on_memo",                        "alert_fn": "memo_positive"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "pipeline",       "label": "Pipeline",         "value_fn": "deal_value_pipeline","sub_fn": "active_deals_sub",     "href": "/crm"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",               "Stock",           "All pieces"),
            ("/inventory?filter=on_memo", "Memo Out",        "Pieces on memo"),
            ("/crm",                      "Client Pipeline", "CRM deals"),
            ("/docs?type=invoice",        "Sales",           "Invoices"),
            ("/docs?type=purchase_order", "Sourcing",        "Purchase orders"),
            ("/reports/ar-aging",         "AR Aging",        "Outstanding receivables"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "artwork": {
        "kpis": [
            {"key": "works_avail",    "label": "Works Available",  "value_fn": "active_items_count","sub_fn": "retail_total_sub",      "href": "/inventory"},
            {"key": "memo_out",       "label": "On Approval",      "value_fn": "memo_balance",      "sub_fn": "memo_count_sub",        "href": "/inventory?filter=on_memo",                        "alert_fn": "memo_positive"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": "gross_sales_sub",       "href": "/reports"},
            {"key": "pipeline",       "label": "Pipeline",         "value_fn": "deal_value_pipeline","sub_fn": "active_deals_sub",     "href": "/crm"},
            {"key": "pos_pending",    "label": "Acquisitions",     "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",               "Works",           "All works"),
            ("/inventory?filter=on_memo", "On Approval",     "Works on approval"),
            ("/crm",                      "Collectors",      "CRM"),
            ("/docs?type=invoice",        "Sales",           "Invoices"),
            ("/reports/ar-aging",         "AR Aging",        "Outstanding receivables"),
            ("/docs?type=purchase_order", "Acquisitions",    "Purchases"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "food_beverage": {
        "kpis": [
            {"key": "expiring",       "label": "Expiring Soon",    "value_fn": "items_expiring_30d","sub_fn": "within_30d_sub",        "href": "/inventory?filter=expiring_soon",                  "alert_fn": "expiring_positive"},
            {"key": "low_stock",      "label": "Low / Out of Stock","value_fn": "low_stock_items",  "sub_fn": "items_at_zero_sub",     "href": "/inventory?filter=low_stock",                      "alert_fn": "low_stock_positive"},
            {"key": "inv_value",      "label": "Inventory Value",  "value_fn": "cost_total",        "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "past_due_sub",          "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order&status=pending"},
        ],
        "secondary_kpis": [
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": None,                    "href": "/reports"},
        ],
        "quick_links": [
            ("/inventory",                "Stock",           "All inventory"),
            ("/inventory?filter=expiring_soon","Expiring Soon","Items expiring soon"),
            ("/inventory?filter=low_stock", "Low Stock",      "Items at zero"),
            ("/docs?type=invoice",         "Orders",          "Sales invoices"),
            ("/docs?type=purchase_order",  "Purchase Orders", "Supplier orders"),
            ("/reports",                   "Reports",         "Analytics"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "fashion": {
        "kpis": [
            {"key": "active_stock",   "label": "Active Stock",     "value_fn": "active_items_count","sub_fn": "retail_total_sub",      "href": "/inventory"},
            {"key": "cost_basis",     "label": "Cost Value",       "value_fn": "cost_total",        "sub_fn": "margin_pct_sub",        "href": "/inventory", "min_role": "manager"},
            {"key": "low_stock",      "label": "Low Stock",        "value_fn": "low_stock_items",   "sub_fn": "items_at_zero_sub",     "href": "/inventory?filter=low_stock",                      "alert_fn": "low_stock_positive"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "past_due_sub",          "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": "ytd_sub",               "href": "/reports"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",                "Stock",           "All inventory"),
            ("/inventory?filter=low_stock","Low Stock",       "Items at zero"),
            ("/docs?type=invoice",         "Sales",           "Invoices"),
            ("/docs?type=purchase_order",  "Purchase Orders", "Supplier orders"),
            ("/crm",                       "Wholesale Accounts","B2B CRM"),
            ("/reports",                   "Reports",         "Analytics"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "electronics": {
        "kpis": [
            {"key": "inv_value",      "label": "Inventory Value",  "value_fn": "retail_total",      "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "cost_basis",     "label": "Cost Basis",       "value_fn": "cost_total",        "sub_fn": "margin_pct_sub",        "href": "/inventory", "min_role": "manager"},
            {"key": "low_stock",      "label": "Low / Out of Stock","value_fn": "low_stock_items",  "sub_fn": "items_at_zero_sub",     "href": "/inventory?filter=low_stock",                      "alert_fn": "low_stock_positive"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "past_due_sub",          "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order&status=pending"},
        ],
        "secondary_kpis": [
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": None,                    "href": "/reports"},
            {"key": "pipeline",       "label": "Active Deals",     "value_fn": "deal_value_pipeline","sub_fn": None,                   "href": "/crm"},
        ],
        "quick_links": [
            ("/inventory",                "Stock",           "All inventory"),
            ("/inventory?filter=low_stock","Low Stock",       "Items at zero"),
            ("/docs?type=invoice",         "Sales",           "Invoices"),
            ("/docs?type=purchase_order",  "Purchases",       "Supplier orders"),
            ("/crm",                       "Accounts",        "CRM"),
            ("/reports/ar-aging",          "AR Aging",        "Outstanding receivables"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "consulting": {
        "kpis": [
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "action_needed_sub",     "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": "ytd_sub",               "href": "/reports"},
            {"key": "pipeline",       "label": "Pipeline Value",   "value_fn": "deal_value_pipeline","sub_fn": "active_deals_sub",     "href": "/crm"},
            {"key": "subscriptions",  "label": "Retainer / Recurring","value_fn": "subscriptions_active","sub_fn": "active_subs_sub",  "href": "/subscriptions"},
            {"key": "ap_outstanding", "label": "AP Outstanding",   "value_fn": "ap_outstanding",    "sub_fn": "vendor_invoices_sub",   "href": "/docs?type=purchase_order"},
        ],
        "secondary_kpis": [
            {"key": "contacts",       "label": "Clients",          "value_fn": "total_contacts",    "sub_fn": None,                    "href": "/crm"},
            {"key": "deals_won",      "label": "Deals Won MTD",    "value_fn": "deals_won_mtd",     "sub_fn": None,                    "href": "/crm"},
        ],
        "quick_links": [
            ("/docs?type=invoice",         "Invoices",        "All invoices"),
            ("/docs?type=invoice&status=overdue","Overdue",   "Past due invoices"),
            ("/crm",                       "Pipeline",        "CRM deals"),
            ("/subscriptions",             "Retainers",       "Recurring subscriptions"),
            ("/docs?type=purchase_order",  "Vendor Bills",    "AP"),
            ("/reports/ar-aging",          "AR Aging",        "Receivables aging"),
        ],
        "charts": ["ar_aging"],
        "show_activity": True,
        "hide_inventory": True,
    },
    "saas": {
        "kpis": [
            {"key": "subscriptions",  "label": "Active Subscriptions","value_fn": "subscriptions_active","sub_fn": "mrr_sub",          "href": "/subscriptions"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "failed_pending_sub",    "href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "Billing Issues",   "value_fn": "ar_overdue",        "sub_fn": "billing_issues_sub",    "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": "ytd_sub",               "href": "/reports"},
            {"key": "pipeline",       "label": "Pipeline",         "value_fn": "deal_value_pipeline","sub_fn": "active_deals_sub",     "href": "/crm"},
            {"key": "deals_won",      "label": "New Customers MTD","value_fn": "deals_won_mtd",     "sub_fn": None,                    "href": "/crm"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/subscriptions",             "Subscriptions",   "All subscriptions"),
            ("/docs?type=invoice",         "Invoices",        "Billing"),
            ("/crm",                       "Pipeline",        "CRM"),
            ("/reports",                   "Reports",         "Analytics"),
            ("/reports/ar-aging",          "AR Aging",        "Receivables"),
            ("/docs?type=invoice&status=overdue","Billing Issues","Failed payments"),
        ],
        "charts": ["ar_aging"],
        "show_activity": True,
        "hide_inventory": True,
    },
    "property_rental": {
        "kpis": [
            {"key": "tenancies",      "label": "Active Tenancies", "value_fn": "subscriptions_active","sub_fn": "occupied_units_sub",  "href": "/subscriptions"},
            {"key": "rent_collected", "label": "Rent Collected MTD","value_fn": "revenue_mtd",       "sub_fn": "ytd_sub",               "href": "/reports"},
            {"key": "ar_overdue",     "label": "Overdue Rent",     "value_fn": "ar_overdue",        "sub_fn": "invoices_past_due_sub", "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "ar_outstanding", "label": "Total Receivable", "value_fn": "ar_outstanding",    "sub_fn": None,                    "href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "maintenance",    "label": "Maintenance Spend","value_fn": "ap_outstanding",    "sub_fn": "open_orders_sub",       "href": "/docs?type=purchase_order"},
            {"key": "pipeline",       "label": "Prospects",        "value_fn": "deal_value_pipeline","sub_fn": "active_deals_sub",     "href": "/crm"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/subscriptions",             "Tenancies",       "Active leases"),
            ("/docs?type=invoice",         "Invoices",        "Rent invoices"),
            ("/docs?type=invoice&status=overdue","Overdue Rent","Past due rent"),
            ("/docs?type=purchase_order",  "Maintenance",     "Repair/maintenance orders"),
            ("/crm",                       "Prospects",       "CRM"),
            ("/reports/ar-aging",          "Rent Aging",      "Receivables aging"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
        "hide_inventory": True,
    },
    "wine_spirits": {
        "kpis": [
            {"key": "stock_value",    "label": "Stock Value",      "value_fn": "retail_total",      "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "cost_basis",     "label": "Cost Basis",       "value_fn": "cost_total",        "sub_fn": "margin_pct_sub",        "href": "/inventory", "min_role": "manager"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "past_due_sub",          "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order&status=pending"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": "ytd_sub",               "href": "/reports"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",                "Stock",           "All stock"),
            ("/docs?type=invoice",         "Sales",           "Invoices"),
            ("/docs?type=purchase_order",  "Purchases",       "Supplier orders"),
            ("/crm",                       "Accounts",        "CRM"),
            ("/reports/ar-aging",          "AR Aging",        "Outstanding receivables"),
            ("/inventory?filter=low_stock","Low Stock",       "Items at zero"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "cosmetics": {
        "kpis": [
            {"key": "expiring",       "label": "Expiring Soon",    "value_fn": "items_expiring_30d","sub_fn": "within_30d_sub",        "href": "/inventory?filter=expiring_soon",                  "alert_fn": "expiring_positive"},
            {"key": "low_stock",      "label": "Low Stock",        "value_fn": "low_stock_items",   "sub_fn": "items_at_zero_sub",     "href": "/inventory?filter=low_stock",                      "alert_fn": "low_stock_positive"},
            {"key": "stock_value",    "label": "Stock Value",      "value_fn": "cost_total",        "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "past_due_sub",          "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": "ytd_sub",               "href": "/reports"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",                "Stock",           "All inventory"),
            ("/inventory?filter=expiring_soon","Expiring Soon","Items expiring soon"),
            ("/inventory?filter=low_stock", "Low Stock",      "Items at zero"),
            ("/docs?type=invoice",         "Sales",           "Invoices"),
            ("/docs?type=purchase_order",  "Purchases",       "Supplier orders"),
            ("/crm",                       "Accounts",        "CRM"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "agricultural": {
        "kpis": [
            {"key": "expiring",       "label": "Expiring Soon",    "value_fn": "items_expiring_30d","sub_fn": "within_30d_sub",        "href": "/inventory?filter=expiring_soon",                  "alert_fn": "expiring_positive"},
            {"key": "stock_value",    "label": "Stock Value",      "value_fn": "cost_total",        "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "past_due_sub",          "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "revenue_ytd",    "label": "Revenue YTD",      "value_fn": "revenue_ytd",       "sub_fn": "mtd_sub",               "href": "/reports"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order&status=pending"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",                "Stock",           "All stock"),
            ("/inventory?filter=expiring_soon","Expiring",   "Items expiring soon"),
            ("/docs?type=invoice",         "Sales",           "Invoices"),
            ("/docs?type=purchase_order",  "Purchases",       "Supplier orders"),
            ("/crm",                       "Buyers",          "CRM"),
            ("/reports",                   "Reports",         "Analytics"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "furniture": {
        "kpis": [
            {"key": "stock_value",    "label": "Stock Value",      "value_fn": "retail_total",      "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "cost_basis",     "label": "Cost Basis",       "value_fn": "cost_total",        "sub_fn": "margin_pct_sub",        "href": "/inventory", "min_role": "manager"},
            {"key": "low_stock",      "label": "Low Stock",        "value_fn": "low_stock_items",   "sub_fn": "items_at_zero_sub",     "href": "/inventory?filter=low_stock",                      "alert_fn": "low_stock_positive"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order&status=pending"},
            {"key": "pipeline",       "label": "Pipeline",         "value_fn": "deal_value_pipeline","sub_fn": "active_deals_sub",     "href": "/crm"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",                "Stock",           "All inventory"),
            ("/inventory?filter=low_stock","Low Stock",       "Items at zero"),
            ("/docs?type=invoice",         "Invoices",        "Sales invoices"),
            ("/docs?type=purchase_order",  "Purchase Orders", "Supplier orders"),
            ("/crm",                       "Sales Pipeline",  "CRM deals"),
            ("/reports/ar-aging",          "AR Aging",        "Outstanding receivables"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "hardware": {
        "kpis": [
            {"key": "low_stock",      "label": "Low / Out of Stock","value_fn": "low_stock_items",  "sub_fn": "reorder_needed_sub",    "href": "/inventory?filter=low_stock",                      "alert_fn": "low_stock_positive"},
            {"key": "stock_value",    "label": "Stock Value",      "value_fn": "cost_total",        "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "past_due_sub",          "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order&status=pending"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": "ytd_sub",               "href": "/reports"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",                "Stock",           "All inventory"),
            ("/inventory?filter=low_stock","Low Stock / Reorder","Items at zero"),
            ("/docs?type=invoice",         "Sales",           "Invoices"),
            ("/docs?type=purchase_order",  "Purchases",       "Supplier orders"),
            ("/crm",                       "Accounts",        "CRM"),
            ("/reports",                   "Reports",         "Analytics"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "books_media": {
        "kpis": [
            {"key": "stock_value",    "label": "Stock Value",      "value_fn": "retail_total",      "sub_fn": "active_titles_sub",     "href": "/inventory"},
            {"key": "cost_basis",     "label": "Cost Basis",       "value_fn": "cost_total",        "sub_fn": "margin_pct_sub",        "href": "/inventory", "min_role": "manager"},
            {"key": "revenue_mtd",    "label": "Revenue MTD",      "value_fn": "revenue_mtd",       "sub_fn": "ytd_sub",               "href": "/reports"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "memo_out",       "label": "Consignment",      "value_fn": "memo_balance",      "sub_fn": "memo_count_sub",        "href": "/inventory?filter=on_memo"},
            {"key": "pos_pending",    "label": "Orders Pending",   "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",                "Stock",           "All titles"),
            ("/docs?type=invoice",         "Sales",           "Invoices"),
            ("/docs?type=purchase_order",  "Orders / Returns","Purchase orders"),
            ("/crm",                       "Accounts",        "CRM"),
            ("/inventory?filter=on_memo",  "Consignment",     "Publisher consignment"),
            ("/reports",                   "Reports",         "Analytics"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
    "automotive": {
        "kpis": [
            {"key": "low_stock",      "label": "Low Stock",        "value_fn": "low_stock_items",   "sub_fn": "items_at_zero_sub",     "href": "/inventory?filter=low_stock",                      "alert_fn": "low_stock_positive"},
            {"key": "stock_value",    "label": "Stock Value",      "value_fn": "retail_total",      "sub_fn": "active_items_sub",      "href": "/inventory"},
            {"key": "cost_basis",     "label": "Cost Basis",       "value_fn": "cost_total",        "sub_fn": "margin_pct_sub",        "href": "/inventory", "min_role": "manager"},
            {"key": "ar_outstanding", "label": "AR Outstanding",   "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding",          "alert_fn": "ar_positive"},
            {"key": "ar_overdue",     "label": "AR Overdue",       "value_fn": "ar_overdue",        "sub_fn": "past_due_sub",          "href": "/docs?type=invoice&status=overdue",                "alert_fn": "ar_overdue_positive"},
            {"key": "pos_pending",    "label": "POs Pending",      "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",    "href": "/docs?type=purchase_order&status=pending"},
        ],
        "secondary_kpis": [],
        "quick_links": [
            ("/inventory",                "Stock",           "All parts/vehicles"),
            ("/inventory?filter=low_stock","Low Stock",       "Items at zero"),
            ("/docs?type=invoice",         "Sales",           "Invoices"),
            ("/docs?type=purchase_order",  "Purchases",       "Supplier orders"),
            ("/crm",                       "Accounts",        "CRM"),
            ("/reports/ar-aging",          "AR Aging",        "Outstanding receivables"),
        ],
        "charts": ["inventory_cat", "ar_aging"],
        "show_activity": True,
    },
}

# Fallback config for blank/unknown verticals
_DEFAULT_CONFIG: dict = {
    "kpis": [
        {"key": "inv_value",      "label": "Inventory (active)",  "value_fn": "active_items_count","sub_fn": "total_items_sub",      "href": "/inventory"},
        {"key": "cost_basis",     "label": "Cost Value",          "value_fn": "cost_total",        "sub_fn": "at_cost_sub",          "href": "/inventory", "min_role": "manager"},
        {"key": "retail_value",   "label": "Retail Value",        "value_fn": "retail_total",      "sub_fn": "at_retail_sub",        "href": "/inventory"},
        {"key": "ar_outstanding", "label": "AR Outstanding",      "value_fn": "ar_outstanding",    "sub_fn": "invoices_outstanding_sub","href": "/docs?type=invoice&status=outstanding", "alert_fn": "ar_positive"},
        {"key": "revenue_mtd",    "label": "Revenue MTD",         "value_fn": "revenue_mtd",       "sub_fn": None,                   "href": "/reports"},
        {"key": "pos_pending",    "label": "POs Pending",         "value_fn": "pending_pos_count", "sub_fn": "ap_outstanding_sub",   "href": "/docs?type=purchase_order"},
    ],
    "secondary_kpis": [],
    "quick_links": [
        ("/inventory",               "Inventory",       "View and manage stock"),
        ("/docs?type=invoice",        "Invoices",        "Sales invoices"),
        ("/docs?type=purchase_order", "Purchase Orders", "Supplier orders"),
        ("/crm",                      "Customers",       "CRM contacts"),
        ("/reports/ar-aging",         "AR Aging",        "Outstanding receivables"),
        ("/accounting",               "Accounts",        "Chart of accounts"),
    ],
    "charts": ["inventory_cat", "ar_aging"],
    "show_activity": True,
}


# ---------------------------------------------------------------------------
# KPI value/sub-label functions
# All take (kpis: dict, currency: str | None) and return a string.
# ---------------------------------------------------------------------------



def _kpi_values(kpis: dict, valuation: dict, doc_summary: dict, memo_summary: dict,
                crm: dict, manufacturing: dict, purchasing: dict, currency: str | None) -> dict:
    """Build a lookup of value_fn/sub_fn name -> rendered string."""
    inv = kpis.get("inventory", {})
    sales = kpis.get("sales", {})
    purch = kpis.get("purchasing", {})
    crm_data = kpis.get("crm", {})
    mfg = kpis.get("manufacturing", {})

    cost_total = float(inv.get("total_value_cost", valuation.get("cost_total", 0)) or 0)
    retail_total = float(inv.get("total_value_retail", valuation.get("retail_total", 0)) or 0)
    active_items = int(inv.get("total_items", valuation.get("active_item_count", valuation.get("item_count", 0))) or 0)
    total_items = int(valuation.get("item_count", active_items) or 0)
    items_expiring = int(inv.get("items_expiring_30d", 0) or 0)
    items_on_memo = int(inv.get("items_on_memo", 0) or 0)
    items_reserved = int(inv.get("items_reserved", 0) or 0)
    low_stock = int(inv.get("low_stock_items", 0) or 0)

    memo_balance = float(memo_summary.get("all_total", 0) or 0)
    memo_count = int(memo_summary.get("active_count", items_on_memo) or 0)

    ar_outstanding = float(sales.get("ar_outstanding", doc_summary.get("ar_outstanding", 0)) or 0)
    ar_overdue = float(sales.get("ar_overdue", 0) or 0)
    ar_total = float(doc_summary.get("ar_gross", 0) or 0)
    invoices_outstanding = int(sales.get("invoices_outstanding", 0) or 0)
    revenue_mtd = float(sales.get("revenue_mtd", 0) or 0)
    revenue_ytd = float(sales.get("revenue_ytd", 0) or 0)

    pending_pos = int(purch.get("pending_pos", purchasing.get("pending_pos", 0)) or 0)
    ap_outstanding = float(purch.get("ap_outstanding", purchasing.get("ap_outstanding", 0)) or 0)

    total_contacts = int(crm_data.get("total_contacts", 0) or 0)
    active_deals = int(crm_data.get("active_deals", 0) or 0)
    deals_won_mtd = int(crm_data.get("deals_won_mtd", 0) or 0)
    deal_value_pipeline = float(crm_data.get("deal_value_pipeline", 0) or 0)

    subscriptions_active = int(kpis.get("subscriptions", {}).get("active_count", memo_summary.get("subscriptions_active", 0)) or 0)

    margin_pct = f"{((retail_total - cost_total) / retail_total * 100):.1f}%" if retail_total > 0 else "n/a"

    return {
        # value functions
        "memo_balance":         _fmt_money(memo_balance, currency),
        "retail_total":         _fmt_money(retail_total, currency),
        "cost_total":           _fmt_money(cost_total, currency),
        "ar_outstanding":       _fmt_money(ar_outstanding, currency),
        "ar_overdue":           _fmt_money(ar_overdue, currency),
        "pending_pos_count":    str(pending_pos),
        "ap_outstanding":       _fmt_money(ap_outstanding, currency),
        "revenue_mtd":          _fmt_money(revenue_mtd, currency),
        "revenue_ytd":          _fmt_money(revenue_ytd, currency),
        "active_items_count":   f"{active_items:,}",
        "low_stock_items":      f"{low_stock:,}",
        "items_expiring_30d":   f"{items_expiring:,}",
        "items_reserved":       f"{items_reserved:,}",
        "deal_value_pipeline":  _fmt_money(deal_value_pipeline, currency),
        "active_deals_count":   str(active_deals),
        "total_contacts":       f"{total_contacts:,}",
        "deals_won_mtd":        str(deals_won_mtd),
        "subscriptions_active": str(subscriptions_active),
        # sub-label functions
        "memo_count_sub":           f"{memo_count} items on consignment",
        "active_items_sub":         f"{active_items:,} items",
        "active_pieces_sub":        f"{active_items:,} pieces",
        "active_titles_sub":        f"{active_items:,} titles",
        "retail_total_sub":         f"{_fmt_money(retail_total, currency)} at retail",
        "margin_pct_sub":           f"margin: {margin_pct}",
        "invoices_outstanding_sub": f"{invoices_outstanding} invoices",
        "past_due_sub":             "past due",
        "ap_outstanding_sub":       f"{_fmt_money(ap_outstanding, currency)} on order",
        "at_cost_sub":              "at cost",
        "at_retail_sub":            "at retail",
        "total_items_sub":          f"{total_items:,} total items",
        "gross_sales_sub":          "gross sales",
        "within_30d_sub":           "within 30 days",
        "items_at_zero_sub":        "items at zero",
        "reorder_needed_sub":       "items at zero - needs reorder",
        "ytd_sub":                  f"{_fmt_money(revenue_ytd, currency)} YTD",
        "mtd_sub":                  f"MTD: {_fmt_money(revenue_mtd, currency)}",
        "active_deals_sub":         f"{active_deals} active deals",
        "active_subs_sub":          "active subscriptions",
        "mrr_sub":                  "active subscriptions",
        "failed_pending_sub":       "failed / pending",
        "billing_issues_sub":       "billing issues",
        "occupied_units_sub":       "occupied units",
        "invoices_past_due_sub":    f"{invoices_outstanding} invoices past due",
        "open_orders_sub":          f"{pending_pos} open orders",
        "action_needed_sub":        "past due - action needed",
        "vendor_invoices_sub":      f"{pending_pos} vendor invoices",
        "memo_exposure_high":       memo_balance > retail_total * 0.4 if retail_total > 0 else False,
        # alert functions (bool values keyed by fn name)
        "ar_positive":          ar_outstanding > 0,
        "ar_overdue_positive":  ar_overdue > 0,
        "memo_positive":        memo_balance > 0,
        "expiring_positive":    items_expiring > 0,
        "low_stock_positive":   low_stock > 0,
    }


# ---------------------------------------------------------------------------
# Route setup
# ---------------------------------------------------------------------------

def setup_routes(app):

    @app.get("/dashboard")
    async def dashboard(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        try:
            company, kpis_data, valuation, doc_summary, memo_summary, companies = await _load_dashboard(token)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if e.status == 404:
                return RedirectResponse("/setup", status_code=302)
            return base_shell(
                page_header("Dashboard"),
                Div(f"Error loading dashboard: {e.detail}", cls="error-banner"),
                title="Dashboard - Celerp",
                nav_active="dashboard",
                request=request,
            )
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).exception("Dashboard load failed: %s", e)
            return base_shell(
                page_header("Dashboard"),
                Div(f"Error loading dashboard: {type(e).__name__}", cls="error-banner"),
                title="Dashboard - Celerp",
                nav_active="dashboard",
                request=request,
            )

        try:
            ar_aging = await api.get_ar_aging(token)
        except APIError:
            ar_aging = {"buckets": {}}

        try:
            activities = await api.get_activity(token, limit=15)
        except Exception:
            activities = []

        vertical = company.get("vertical") or ""
        cfg = _VERTICAL_CONFIGS.get(vertical, _DEFAULT_CONFIG)
        currency = company.get("currency")
        lang = get_lang(request)

        crm_data = kpis_data.get("crm", {})
        mfg_data = kpis_data.get("manufacturing", {})
        purchasing_data = kpis_data.get("purchasing", {})

        values = _kpi_values(kpis_data, valuation, doc_summary, memo_summary,
                             crm_data, mfg_data, purchasing_data, currency)

        role = _get_role(request)
        return base_shell(
            page_header(t("page.dashboard", lang)),
            _kpi_grid(cfg, values, role=role),
            _secondary_kpi_grid(cfg, values, role=role),
            _charts_section(cfg, valuation, ar_aging),
            _activity_feed(activities, currency) if cfg.get("show_activity") else "",
            _quick_links(cfg),
            title=f"Dashboard - {company.get('name', '')}",
            nav_active="dashboard",
            companies=companies,
            lang=lang,
            request=request,
        )


async def _load_dashboard(token: str):
    company = await api.get_company(token)

    async def _safe(coro, default):
        try:
            return await coro
        except (APIError, Exception):
            return default

    valuation = await _safe(api.get_valuation(token), {})
    doc_summary = await _safe(api.get_doc_summary(token), {})
    memo_summary = await _safe(api.get_memo_summary(token), {})
    kpis_data = await _safe(api.get_dashboard_kpis(token), {})
    try:
        companies_resp = await api.my_companies(token)
        companies = companies_resp.get("items", []) if isinstance(companies_resp, dict) else companies_resp
    except APIError:
        companies = []
    return company, kpis_data, valuation, doc_summary, memo_summary, companies


# ---------------------------------------------------------------------------
# KPI rendering
# ---------------------------------------------------------------------------

def _kpi_card(spec: dict, values: dict) -> FT:
    label = spec["label"]
    value_key = spec["value_fn"]
    sub_key = spec.get("sub_fn")
    href = spec.get("href")
    alert_key = spec.get("alert_fn")

    value_str = values.get(value_key, "-")
    sub_str = values.get(sub_key, "") if sub_key else ""
    is_alert = bool(values.get(alert_key)) if alert_key else False

    card_cls = "kpi-card" + (" kpi-card--alert" if is_alert else "")
    inner = Div(
        P(label, cls="kpi-label"),
        P(value_str, cls="kpi-value"),
        P(sub_str, cls="kpi-sub") if sub_str else "",
    )
    if href:
        return A(inner, href=href, cls=card_cls)
    return Div(inner, cls=card_cls)


def _kpi_grid(cfg: dict, values: dict, role: str = "owner") -> FT:
    from celerp.services.auth import ROLE_LEVELS
    user_level = ROLE_LEVELS.get(role, ROLE_LEVELS["owner"])
    cards = [_kpi_card(spec, values) for spec in cfg.get("kpis", [])
             if user_level >= ROLE_LEVELS.get(spec.get("min_role", "viewer"), 1)]
    return Div(*cards, cls="kpi-grid")


def _secondary_kpi_grid(cfg: dict, values: dict, role: str = "owner") -> FT:
    from celerp.services.auth import ROLE_LEVELS
    user_level = ROLE_LEVELS.get(role, ROLE_LEVELS["owner"])
    secondary = cfg.get("secondary_kpis", [])
    if not secondary:
        return ""
    cards = [_kpi_card(spec, values) for spec in secondary
             if user_level >= ROLE_LEVELS.get(spec.get("min_role", "viewer"), 1)]
    if not cards:
        return ""
    return Div(*cards, cls="kpi-grid kpi-grid--secondary")


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

_CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"


def _charts_section(cfg: dict, valuation: dict, ar_aging: dict) -> FT:
    import json
    show_charts = cfg.get("charts", [])
    if not show_charts:
        return ""

    buckets = ar_aging.get("buckets", {})
    ar_labels = json.dumps(list(buckets.keys())) if buckets else "[]"
    ar_data = json.dumps([float(v) for v in buckets.values()]) if buckets else "[]"

    cats = valuation.get("category_counts", {})
    cat_labels = json.dumps(list(cats.keys())) if cats else "[]"
    cat_data = json.dumps([int(v) for v in cats.values()]) if cats else "[]"

    chart_cards = []
    if "ar_aging" in show_charts:
        chart_cards.append(
            Div(
                H3(t("page.ar_aging"), cls="chart-title"),
                Canvas(id="chart-ar-aging", width="300", height="300"),
                cls="chart-card",
            )
        )
    if "inventory_cat" in show_charts:
        chart_cards.append(
            Div(
                H3(t("page.inventory_by_category"), cls="chart-title"),
                Canvas(id="chart-inventory-cat", width="400", height="300"),
                cls="chart-card",
            )
        )

    if not chart_cards:
        return ""

    chart_init = f"""
    function initDashboardCharts() {{
      if (typeof Chart === 'undefined') {{ setTimeout(initDashboardCharts, 100); return; }}
      var style = getComputedStyle(document.documentElement);
      var colors = [
        style.getPropertyValue('--c-chart-1').trim() || '#4f8ef7',
        style.getPropertyValue('--c-chart-2').trim() || '#f7c948',
        style.getPropertyValue('--c-chart-3').trim() || '#e05252',
        style.getPropertyValue('--c-chart-4').trim() || '#5ec269',
        style.getPropertyValue('--c-chart-5').trim() || '#b07ce8',
        style.getPropertyValue('--c-chart-6').trim() || '#e8913c'
      ];
      var textColor = style.getPropertyValue('--c-text').trim() || '#ccc';
      var gridColor = style.getPropertyValue('--c-border').trim() || '#333';

      var arCtx = document.getElementById('chart-ar-aging');
      if (arCtx && {ar_labels}.length > 0) {{
        new Chart(arCtx, {{
          type: 'doughnut',
          data: {{ labels: {ar_labels}, datasets: [{{ data: {ar_data}, backgroundColor: colors }}] }},
          options: {{ responsive: true, plugins: {{ legend: {{ labels: {{ color: textColor }} }} }} }}
        }});
      }}

      var catCtx = document.getElementById('chart-inventory-cat');
      if (catCtx && {cat_labels}.length > 0) {{
        new Chart(catCtx, {{
          type: 'bar',
          data: {{ labels: {cat_labels}, datasets: [{{ label: 'Items', data: {cat_data}, backgroundColor: colors[0] }}] }},
          options: {{
            indexAxis: 'y', responsive: true,
            scales: {{ x: {{ ticks: {{ color: textColor }}, grid: {{ color: gridColor }} }}, y: {{ ticks: {{ color: textColor }}, grid: {{ display: false }} }} }},
            plugins: {{ legend: {{ display: false }} }}
          }}
        }});
      }}
    }}
    initDashboardCharts();
    """

    return Div(
        H2(t("page.charts"), cls="section-title"),
        Div(*chart_cards, cls="charts-grid"),
        Script(src=_CHARTJS_CDN),
        Script(chart_init),
        cls="charts-section",
    )


# ---------------------------------------------------------------------------
# Activity feed
# ---------------------------------------------------------------------------

def _activity_feed(activities: list[dict], currency: str | None = None) -> FT:
    from ui.components.activity import activity_table
    if not activities:
        return ""
    return activity_table(activities, max_display=15)



# ---------------------------------------------------------------------------
# Quick links
# ---------------------------------------------------------------------------

def _quick_links(cfg: dict) -> FT:
    links = cfg.get("quick_links", [])
    if not links:
        return ""
    return Div(
        H2(t("page.quick_links"), cls="section-title"),
        Div(
            *[
                A(
                    Strong(label),
                    P(desc, cls="quick-link-desc"),
                    href=href,
                    cls="quick-link-card",
                )
                for href, label, desc in links
            ],
            cls="quick-links-grid",
        ),
        cls="quick-links-section",
    )
