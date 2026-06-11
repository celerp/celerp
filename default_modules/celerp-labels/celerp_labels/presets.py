# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Default label templates, shared by the API (seed-on-first-read) and the labels UI.

Single source of truth so every consumer — the inventory/doc print-label dropdowns and the
labels settings page — sees the same presets without anyone having to visit a particular page.
"""
from __future__ import annotations

PRESET_TEMPLATES: list[dict] = [
    # -- Small square barcode stickers --
    {
        "name": "Barcode Sticker (24x24)",
        "format": "24x24mm",
        "fields": [
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 1, "y": 1, "fontSize": 6},
            {"key": "name", "label": "Name", "type": "text", "x": 1, "y": 16, "fontSize": 5},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 1, "y": 20, "fontSize": 5},
        ],
    },
    {
        "name": "Barcode Sticker (29x29)",
        "format": "29x29mm",
        "fields": [
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 1, "y": 1, "fontSize": 7},
            {"key": "name", "label": "Name", "type": "text", "x": 1, "y": 17, "fontSize": 6},
            {"key": "sku", "label": "SKU", "type": "text", "x": 1, "y": 22, "fontSize": 5},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 1, "y": 26, "fontSize": 5},
        ],
    },
    {
        "name": "Barcode Sticker (34x34)",
        "format": "34x34mm",
        "fields": [
            {"key": "name", "label": "Name", "type": "text", "x": 2, "y": 2, "fontSize": 7},
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 2, "y": 8, "fontSize": 7},
            {"key": "sku", "label": "SKU", "type": "text", "x": 2, "y": 24, "fontSize": 5},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 2, "y": 28, "fontSize": 6},
        ],
    },
    # -- Rectangular labels --
    {
        "name": "Small Tag (40x30)",
        "format": "40x30mm",
        "fields": [
            {"key": "name", "label": "Name", "type": "text", "x": 2, "y": 2, "fontSize": 7},
            {"key": "sku", "label": "SKU", "type": "text", "x": 2, "y": 7, "fontSize": 5},
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 2, "y": 12, "fontSize": 7},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 2, "y": 26, "fontSize": 7},
        ],
    },
    {
        "name": "QR Label (62x29)",
        "format": "62x29mm",
        "fields": [
            {"key": "qr", "label": "QR Code", "type": "qr", "x": 2, "y": 2, "fontSize": 7},
            {"key": "name", "label": "Name", "type": "text", "x": 14, "y": 2, "fontSize": 7},
            {"key": "sku", "label": "SKU", "type": "text", "x": 14, "y": 8, "fontSize": 5},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 14, "y": 13, "fontSize": 8},
            {"key": "category", "label": "Category", "type": "text", "x": 14, "y": 20, "fontSize": 5},
        ],
    },
    {
        "name": "Shelf Label (100x50)",
        "format": "100x50mm",
        "fields": [
            {"key": "name", "label": "Name", "type": "text", "x": 3, "y": 3, "fontSize": 10},
            {"key": "sku", "label": "SKU", "type": "text", "x": 3, "y": 12, "fontSize": 6},
            {"key": "category", "label": "Category", "type": "text", "x": 3, "y": 17, "fontSize": 6},
            {"key": "barcode", "label": "Barcode", "type": "barcode", "x": 3, "y": 23, "fontSize": 8},
            {"key": "sale_price", "label": "Sale Price", "type": "text", "x": 60, "y": 3, "fontSize": 12},
            {"key": "location_name", "label": "Location", "type": "text", "x": 60, "y": 14, "fontSize": 6},
            {"key": "qr", "label": "QR Code", "type": "qr", "x": 60, "y": 23, "fontSize": 7},
        ],
    },
]
