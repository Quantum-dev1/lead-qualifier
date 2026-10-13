from datetime import date

import pytest

from lead_qualifier.extract import find_money, parse_budget, parse_company_size, parse_timeline

FX = {"USD": 1.0, "EUR": 1.1, "GBP": 1.25}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Our budget is $80k.", 80_000),
        ("We have budgeted EUR 120,000 for this", 132_000),
        ("between 15,000 and 25,000 USD", 20_000),
        ("$20k-$40k", 30_000),
        ("roughly 20-30k dollars", 25_000),
        ("£50,000", 62_500),
        ("budget around 1.5m", 1_500_000),
        ("We can spend $5k/month on a retainer", 60_000),
    ],
)
def test_parse_budget(text: str, expected: float) -> None:
    amount, matched = parse_budget(text, fx=FX)
    assert amount == pytest.approx(expected)
    assert matched


@pytest.mark.parametrize(
    "text",
    [
        "We have 50k users and 200 employees",  # no currency, not near a budget word
        "Founded in 2019, launching in 2027",
        "Our developers cost $40/hour",  # hourly rate, not a budget
        "It costs $5",
    ],
)
def test_parse_budget_ignores_non_budgets(text: str) -> None:
    assert parse_budget(text, fx=FX) == (None, None)


def test_budget_field_accepts_bare_numbers() -> None:
    assert parse_budget("60000", fx=FX, assume_money=True)[0] == 60_000


def test_budget_prefers_amount_nearest_to_budget_keyword() -> None:
    amount, _ = parse_budget("We raised $2M last year. Our budget for this project is $45k.", fx=FX)
    assert amount == 45_000


def test_unknown_currency_is_not_converted() -> None:
    assert find_money("CHF 10,000")[0].currency == "CHF"
    assert parse_budget("CHF 10,000", fx=FX) == (None, "CHF 10,000")


TODAY = date(2026, 10, 14)


@pytest.mark.parametrize(
    ("text", "months", "exploring"),
    [
        ("We need this ASAP", 0.5, False),
        ("launch within 6 weeks", 1.5, False),
        ("in 3 months", 3.0, False),
        ("start next month", 1.0, False),
        ("go live by March", 5.0, False),
        ("ready for Q1", 5.0, False),
        ("we are just exploring options", None, True),
        ("no timeline yet", None, True),
        ("we sell widgets", None, False),
    ],
)
def test_parse_timeline(text: str, months: float | None, exploring: bool) -> None:
    parsed_months, _, is_exploring = parse_timeline(text, TODAY)
    assert parsed_months == months
    assert is_exploring is exploring


@pytest.mark.parametrize(
    ("text", "size"),
    [
        ("a 120-person company", 120),
        ("50-200 employees", 200),
        ("team of 8", 8),
        ("I'm a solo founder", 1),
        ("a Fortune 500 retailer", 1000),
        ("we sell 300 products", None),
    ],
)
def test_parse_company_size(text: str, size: int | None) -> None:
    assert parse_company_size(text)[0] == size
