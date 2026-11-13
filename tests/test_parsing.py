from harbor.parsing import (
    is_explicit_confirmation,
    looks_like_new_order,
    parse_items,
)
from harbor.catalog import load_catalog


def test_same_sentence_order_and_yes_is_not_confirmation():
    text = "I'd like 2 coastal linen shirts in medium. Yes I confirm."
    assert looks_like_new_order(text)
    assert is_explicit_confirmation(text, "ABCD") is False


def test_yes_on_its_own_is_confirmation():
    assert is_explicit_confirmation("Yes, I confirm")
    assert is_explicit_confirmation("YES")
    assert is_explicit_confirmation("place the order")
    assert is_explicit_confirmation("token ABCD", "ABCD")


def test_bare_ok_is_not_enough():
    assert is_explicit_confirmation("ok") is False
    assert is_explicit_confirmation("okay") is False


def test_parse_shirt_size_and_quantity():
    items = parse_items("I'd like 2 coastal linen shirts in medium", load_catalog()["products"])
    assert len(items) == 1
    assert items[0].quantity == 2
    assert items[0].needs_size is False
    assert items[0].variant["title"] == "M"
    assert items[0].product["title"] == "Coastal Linen Shirt"


def test_sized_product_without_a_size_is_unresolved():
    items = parse_items("I'd like 2 coastal linen shirts", load_catalog()["products"])
    assert items[0].needs_size is True
    assert "M" in items[0].options
