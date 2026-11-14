# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Every translation of a string that names a navigation or document type term uses the same
word for it as the side menu and the document type labels in that language.

GLOSSARY maps each English term to the pattern of its one translation per locale. The pattern
covers the grammatical forms of that one word (plural, case, a joined preposition), never a
second word. A string whose English source contains the term passes when its translation
matches, or when it still carries the English text while it waits for its translation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_CATALOG_DIRS = [ROOT / "ui" / "locales", *sorted(ROOT.glob("default_modules/*/*/locales"))]

GLOSSARY: dict[str, dict[str, str]] = {
    "Remote Access": {
        "am": "ርቀት መዳረሻ", "ar": "الوصول عن بُعد", "de": "Fernzugriff", "es": "acceso remoto",
        "fr": "accès à distance", "id": "akses jarak jauh", "it": "accesso remoto", "ja": "リモートアクセス",
        "pt": "acesso remoto", "th": "การเข้าถึงระยะไกล", "vi": "truy cập từ xa",
    },
    "Vendor Credit Memo": {
        "am": "አቅራቢ ክሬዲት ማስታወሻ", "ar": r"إشعار\S* (ال)?دائن\S*( جديد)? من المورّد|إشعارات المورّد الدائنة",
        "de": "lieferantengutschrift", "es": r"notas? de crédito de(l)? proveedor", "fr": r"avoirs? fournisseurs?",
        "id": "nota kredit pemasok", "it": r"not[ae] di credito (del |dei )?fornitor", "ja": "仕入先クレジットメモ",
        "pt": r"notas? de crédito de(o)? fornecedor", "th": "ใบลดหนี้จากผู้จำหน่าย", "vi": r"phiếu ghi có (của |từ )?nhà cung cấp",
    },
    "Credit Memo": {
        "am": "ክሬዲት ማስታወሻ", "ar": r"إشعار\S* (ال)?دائن", "de": "gutschrift", "es": r"notas? de crédito",
        "fr": r"avoirs?\b", "id": "nota kredit", "it": r"not[ae] di credito", "ja": "クレジットメモ",
        "pt": r"notas? de crédito", "th": "ใบลดหนี้", "vi": "phiếu ghi có",
    },
    "Quotation": {
        "am": "ዋጋ ጥቅ(ስ|ሶች)", "ar": r"عرو?ض (ال)?(أسعار|سعر)", "de": "angebot", "es": "presupuesto", "fr": "devis",
        "id": "penawaran", "it": "preventiv", "ja": "見積", "pt": "cotaç", "th": "ใบเสนอราคา", "vi": "báo giá",
    },
    "Production Order": {
        "am": "ምርት ትዕዛ", "ar": r"(أمر|أوامر) (ال)?إنتاج", "de": "produktionsauftr", "es": r"(orden|órdenes) de producción",
        "fr": r"ordres? de production", "id": "perintah produksi", "it": r"ordin[ei] di produzione", "ja": "製造指図書",
        "pt": r"orde(m|ns) de produção", "th": "คำสั่งผลิต", "vi": "lệnh sản xuất",
    },
    "Stock Count": {
        "am": "ቆጠራ", "ar": "جرد", "de": "inventur", "es": "recuento", "fr": "inventaire", "id": "penghitungan",
        "it": "inventari", "ja": "棚卸", "pt": r"contage(m|ns)", "th": "ตรวจนับ", "vi": "kiểm kê",
    },
    "Shared Documents": {
        "am": "የተጋሩ ሰነዶች", "ar": "المستندات المشتركة", "de": "geteilte dokumente", "es": "documentos compartidos",
        "fr": "documents partagés", "id": "dokumen bersama", "it": "documenti condivisi", "ja": "共有文書",
        "pt": "documentos compartilhados", "th": "เอกสารที่แชร์", "vi": "chứng từ chia sẻ",
    },
    "System Health Check": {
        "am": "የስርዓት ጤና ምርመራ", "ar": "فحص سلامة النظام", "de": "systemzustandsprüfung",
        "es": "comprobación del estado del sistema", "fr": "vérification de l'état du système",
        "id": "pemeriksaan kesehatan sistem", "it": "controllo integrità del sistema", "ja": "システム健全性チェック",
        "pt": "verificação de integridade do sistema", "th": "ตรวจสุขภาพระบบ", "vi": "kiểm tra sức khỏe hệ thống",
    },
    "Transfer Order": {
        "am": "ማስተላለፍ ትዕዛዝ", "ar": r"(أمر|أوامر) (ال)?نقل", "de": "umlagerungsauftr", "es": r"(orden|órdenes) de traslado",
        "fr": r"ordres? de transfert", "id": "order transfer", "it": r"ordin[ei] di trasferimento", "ja": "移動指示",
        "pt": r"orde(m|ns) de transferência", "th": "ใบสั่งโอนย้าย", "vi": "lệnh chuyển kho",
    },
    "Inventory Adjustment": {
        "am": "ክምችት ማስተካከያ", "ar": r"تسوي(ة|ات) (ال)?مخزون", "de": "bestandsanpassung", "es": r"ajustes? de inventario",
        "fr": r"ajustements? de stock", "id": "penyesuaian persediaan", "it": r"rettific(a|he) di magazzino",
        "ja": "在庫調整", "pt": r"ajustes? de estoque", "th": "ปรับปรุงสินค้าคงคลัง", "vi": "điều chỉnh tồn kho",
    },
}

# The labels each term is taken from: the side menu and the document type names.
_LABELS = {
    "Remote Access": ["settings_cloud.web_access", "msg._web_access"],
    "Vendor Credit Memo": ["enum.doc_type.debit_note", "nav.supplier_credit_notes"],
    "Credit Memo": ["enum.doc_type.credit_note", "nav.credit_notes"],
    "Quotation": ["enum.doc_type.list", "nav.lists"],
    "Production Order": ["enum.doc_type.production_order", "documents.page_stock_orders"],
    "Stock Count": ["enum.list_type.audit", "nav.audits"],
    "Shared Documents": ["nav.received_documents"],
    "System Health Check": ["nav.doctor", "doctor.title"],
    "Transfer Order": ["enum.list_type.transfer"],
    "Inventory Adjustment": ["enum.list_type.writeoff"],
}

# Strings whose English uses the words in another sense: the connector sync row "Stock counts"
# means quantities on hand, and every translation says so.
_OTHER_SENSE = {("Stock Count", "connectors.flow_inventory")}


def _catalogs(d: Path) -> dict[str, dict[str, str]]:
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))}


def _names(term: str, english: str) -> bool:
    """The English text names the term (a longer glossary term containing it does not count)."""
    text = english.lower()
    for other in GLOSSARY:
        if other != term and term.lower() in other.lower():
            text = text.replace(other.lower(), "")
    return re.search(rf"\b{re.escape(term.lower())}", text) is not None


def _misworded(term: str) -> list[tuple[str, str, str]]:
    out = []
    for d in _CATALOG_DIRS:
        cats = _catalogs(d)
        en = cats.get("en", {})
        for key, english in en.items():
            if not isinstance(english, str) or (term, key) in _OTHER_SENSE or not _names(term, english):
                continue
            for code, pattern in GLOSSARY[term].items():
                text = cats.get(code, {}).get(key)
                if text is None or text == english:
                    continue
                if not re.search(pattern, text, re.IGNORECASE):
                    out.append((d.relative_to(ROOT).as_posix(), code, key))
    return out


def test_glossary_covers_every_locale():
    codes = sorted(p.stem for p in (ROOT / "ui" / "locales").glob("*.json") if p.stem != "en")
    assert {term: sorted(m) for term, m in GLOSSARY.items()} == {term: codes for term in GLOSSARY}
    assert set(_LABELS) == set(GLOSSARY)


@pytest.mark.parametrize("term", list(GLOSSARY))
def test_labels_use_the_glossary_term(term):
    """The side menu and document type labels themselves carry the term."""
    cats = _catalogs(ROOT / "ui" / "locales")
    bad = [(code, key, cats[code][key]) for key in _LABELS[term] for code, pattern in GLOSSARY[term].items()
           if cats[code][key] != cats["en"][key] and not re.search(pattern, cats[code][key], re.IGNORECASE)]
    assert bad == []


@pytest.mark.parametrize("term", list(GLOSSARY))
def test_every_translation_naming_the_term_uses_its_one_translation(term):
    assert _misworded(term) == []


def test_a_second_word_for_a_term_is_caught():
    """The check itself: a Thai string naming Remote Access in English, or by the old Web Access
    word, is reported."""
    assert re.search(GLOSSARY["Remote Access"]["th"], "ใน Remote Access") is None
    assert re.search(GLOSSARY["Remote Access"]["th"], "ในการเข้าถึงเว็บ") is None
    assert re.search(GLOSSARY["Remote Access"]["th"], "ในการเข้าถึงระยะไกล") is not None
    assert _names("Credit Memo", "Ask for a vendor credit memo") is False
    assert _names("Credit Memo", "Issue a credit memo") is True
