"""The New Bill button reads differently from New Invoice in every language, and the Arabic
bill label is not the invoice word with the definite article."""
import pytest

from ui.i18n import available_langs, t


@pytest.mark.parametrize("lang", available_langs())
def test_new_bill_button_is_not_new_invoice(lang):
    assert t("btn.new_bill", lang).casefold() != t("btn.new_invoice", lang).casefold(), \
        (lang, t("btn.new_bill", lang))


def test_arabic_bill_is_not_the_invoice():
    bill, inv = t("settings.doc_type_bill", "ar"), t("settings.doc_type_invoice", "ar")
    assert bill.removeprefix("ال") != inv, (bill, inv)
