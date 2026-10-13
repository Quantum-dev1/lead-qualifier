"""Deterministic field extraction: budget, timeline, company size, industry and need.

These parsers are deliberately conservative. When they are not sure they return
``None`` and the LLM (if configured) fills the gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}
CURRENCY_WORDS = {
    "usd": "USD",
    "dollars": "USD",
    "dollar": "USD",
    "eur": "EUR",
    "euro": "EUR",
    "euros": "EUR",
    "gbp": "GBP",
    "pounds": "GBP",
    "cad": "CAD",
    "aud": "AUD",
    "chf": "CHF",
}
MULTIPLIERS = {"k": 1_000, "thousand": 1_000, "m": 1_000_000, "mm": 1_000_000, "million": 1_000_000}

_CUR = r"(?:[$€£]|\b(?:usd|eur|gbp|cad|aud|chf)\b)"
_CUR_AFTER = r"(?:usd|eur|gbp|cad|aud|chf|dollars?|euros?|pounds)\b"
_NUM = r"\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_SUF = r"(?:k|mm|m|thousand|million)\b"

AMOUNT_RE = re.compile(
    rf"(?P<cur1>{_CUR})?\s?(?P<num>{_NUM})\s?(?P<suf>{_SUF})?\s?(?P<cur2>{_CUR_AFTER})?",
    re.IGNORECASE,
)
RANGE_RE = re.compile(
    rf"(?P<cur1>{_CUR})?\s?(?P<a>{_NUM})\s?(?P<suf1>{_SUF})?\s?(?:-|–|—|to|and)\s?"
    rf"(?P<cur2>{_CUR})?\s?(?P<b>{_NUM})\s?(?P<suf2>{_SUF})?\s?(?P<cur3>{_CUR_AFTER})?",
    re.IGNORECASE,
)
MONTHLY_RE = re.compile(r"^\s*(?:/|per|a)\s*(?:mo|month)\b|^\s*monthly\b", re.IGNORECASE)
HOURLY_RE = re.compile(r"^\s*(?:/|per|an?)\s*(?:h|hr|hour)\b|^\s*hourly\b", re.IGNORECASE)
BUDGET_KEYWORDS = re.compile(
    r"\b(budget|budgeted|spend|invest|price range|up to|around|approx)", re.IGNORECASE
)


@dataclass(frozen=True)
class Money:
    amount: float
    currency: str
    text: str
    position: int
    monthly: bool = False


def _to_number(raw: str) -> float:
    return float(re.sub(r"[,\s]", "", raw))


def _currency(*tokens: str | None) -> str | None:
    for token in tokens:
        if not token:
            continue
        token = token.strip().lower()
        if token in CURRENCY_SYMBOLS:
            return CURRENCY_SYMBOLS[token]
        if token in CURRENCY_WORDS:
            return CURRENCY_WORDS[token]
    return None


def _scale(value: float, suffix: str | None) -> float:
    return value * MULTIPLIERS.get((suffix or "").lower(), 1)


def find_money(text: str, *, assume_money: bool = False, default_currency: str = "USD") -> list[Money]:
    """Find money amounts. Bare numbers only count when ``assume_money`` is set (e.g. a budget field)."""
    found: list[Money] = []
    taken: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < e and s < end for s, e in taken)

    for m in RANGE_RE.finditer(text):
        cur = _currency(m["cur1"], m["cur2"], m["cur3"])
        suf2 = m["suf2"]
        if not (cur or suf2 or m["suf1"] or assume_money):
            continue
        low = _scale(_to_number(m["a"]), m["suf1"] or suf2)
        high = _scale(_to_number(m["b"]), suf2)
        if low > high or high < 100:
            continue
        tail = text[m.end() : m.end() + 15]
        if HOURLY_RE.search(tail):
            continue
        found.append(
            Money(
                (low + high) / 2,
                cur or default_currency,
                m.group(0).strip(),
                m.start(),
                bool(MONTHLY_RE.search(tail)),
            )
        )
        taken.append((m.start(), m.end()))

    for m in AMOUNT_RE.finditer(text):
        if overlaps(m.start(), m.end()) or not m["num"]:
            continue
        cur = _currency(m["cur1"], m["cur2"])
        suffix = m["suf"]
        near_budget_word = bool(BUDGET_KEYWORDS.search(text[max(0, m.start() - 40) : m.start()]))
        if not (cur or assume_money or (suffix and near_budget_word)):
            continue
        amount = _scale(_to_number(m["num"]), suffix)
        if amount < 100:  # "$5", "3 euros": not a project budget
            continue
        # Skip years and other 4-digit numbers that are not money.
        if not cur and not suffix and 1900 <= amount <= 2100:
            continue
        tail = text[m.end() : m.end() + 15]
        if HOURLY_RE.search(tail):
            continue
        found.append(
            Money(
                amount, cur or default_currency, m.group(0).strip(), m.start(), bool(MONTHLY_RE.search(tail))
            )
        )
    return sorted(found, key=lambda money: money.position)


def parse_budget(
    text: str, *, fx: dict[str, float], assume_money: bool = False, months_for_retainer: int = 12
) -> tuple[float | None, str | None]:
    """Return (amount converted with ``fx``, matched text). Monthly amounts are annualized."""
    candidates = find_money(text, assume_money=assume_money)
    if not candidates:
        return None, None
    keyword_positions = [m.start() for m in BUDGET_KEYWORDS.finditer(text)]
    if keyword_positions:
        # "budget is $45k" is far more common than "$45k budget": amounts after the keyword win.
        def distance(c: Money) -> float:
            return min(
                (c.position - k) if c.position >= k else (k - c.position) * 3 for k in keyword_positions
            )

        best = min(candidates, key=distance)
    else:
        best = max(candidates, key=lambda c: c.amount)
    rate = fx.get(best.currency)
    if rate is None:
        return None, best.text
    amount = best.amount * rate * (months_for_retainer if best.monthly else 1)
    return round(amount, 2), best.text + (" (monthly)" if best.monthly else "")


# --- timeline -----------------------------------------------------------------

MONTHS = {
    name: index + 1
    for index, name in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]
    )
}
UNIT_IN_MONTHS = {"day": 1 / 30, "week": 0.25, "month": 1.0, "year": 12.0}
WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twelve": 12,
    "a": 1,
    "a couple of": 2,
    "a few": 3,
}

URGENT_RE = re.compile(
    r"\b(asap|urgent(ly)?|immediately|right away|this week|yesterday|as soon as possible)\b", re.IGNORECASE
)
EXPLORING_RE = re.compile(
    r"\b(just exploring|just curious|no rush|no timeline|some ?day|researching options|not sure when|"
    r"early idea|just an idea|thinking about it|no fixed date|maybe next year)\b",
    re.IGNORECASE,
)
DURATION_RE = re.compile(
    r"\b(?:in|within|next|over the next|by|about|around|under|less than)\s+"
    r"(?P<n>\d+(?:\.\d+)?|one|two|three|four|five|six|eight|nine|ten|twelve|a couple of|a few|a)\s*"
    r"(?P<unit>day|week|month|year)s?\b",
    re.IGNORECASE,
)
QUARTER_RE = re.compile(r"\b(?:q(?P<q>[1-4])|next quarter|this quarter)\b", re.IGNORECASE)
BY_MONTH_RE = re.compile(
    r"\b(?:by|before|in|until|for|launch in|live in)\s+(?:the\s+)?(?:end of\s+)?(?P<month>"
    + "|".join(MONTHS)
    + r")\b",
    re.IGNORECASE,
)


def parse_timeline(text: str, today: date | None = None) -> tuple[float | None, str | None, bool]:
    """Return (months until the lead wants results, matched text, is_exploring)."""
    today = today or date.today()
    if m := EXPLORING_RE.search(text):
        return None, m.group(0), True
    if m := URGENT_RE.search(text):
        return 0.5, m.group(0), False
    if m := DURATION_RE.search(text):
        raw = m["n"].lower()
        n = WORD_NUMBERS.get(raw)
        number = float(n) if n is not None else float(raw)
        return round(number * UNIT_IN_MONTHS[m["unit"].lower()], 2), m.group(0), False
    if m := BY_MONTH_RE.search(text):
        month = MONTHS[m["month"].lower()]
        delta = (month - today.month) % 12 or 12
        return float(delta), m.group(0), False
    if m := QUARTER_RE.search(text):
        phrase = m.group(0).lower()
        if phrase == "this quarter":
            return 2.0, m.group(0), False
        if phrase == "next quarter":
            return 4.0, m.group(0), False
        quarter_end_month = int(m["q"]) * 3
        delta = (quarter_end_month - today.month) % 12 or 12
        return float(delta), m.group(0), False
    lowered = text.lower()
    for phrase, months in (
        ("next month", 1.0),
        ("this month", 0.75),
        ("end of the year", 6.0),
        ("this year", 6.0),
        ("next year", 12.0),
    ):
        if phrase in lowered:
            return months, phrase, False
    return None, None, False


# --- company size ---------------------------------------------------------------

SIZE_RE = re.compile(
    r"(?P<a>\d{1,3}(?:,\d{3})*|\d+)\s*(?:\+|-|–|to)?\s*(?P<b>\d{1,3}(?:,\d{3})*|\d+)?\s*\+?\s*"
    r"(?:-?\s*(?:person|people|employees|staff|engineers|developers|team members|fte)\b)",
    re.IGNORECASE,
)
TEAM_OF_RE = re.compile(r"\bteam of (?P<n>\d+)", re.IGNORECASE)
SOLO_RE = re.compile(
    r"\b(solo founder|just me|one-person|freelancer|side project|individual)\b", re.IGNORECASE
)
LARGE_RE = re.compile(r"\b(enterprise|fortune 500|multinational|publicly traded)\b", re.IGNORECASE)


def parse_company_size(text: str) -> tuple[int | None, str | None]:
    if m := SIZE_RE.search(text):
        a = int(m["a"].replace(",", ""))
        b = int(m["b"].replace(",", "")) if m["b"] else None
        return (b if b and b > a else a), m.group(0).strip()
    if m := TEAM_OF_RE.search(text):
        return int(m["n"]), m.group(0)
    if m := SOLO_RE.search(text):
        return 1, m.group(0)
    if m := LARGE_RE.search(text):
        return 1000, m.group(0)
    return None, None


# --- keyword helpers ------------------------------------------------------------------


@lru_cache(maxsize=2048)
def keyword_regex(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword.strip()).replace(r"\ ", r"[\s-]+")
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


def matched_keywords(text: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if keyword_regex(kw).search(text)]


def best_category(text: str, categories: dict[str, list[str]]) -> tuple[str | None, list[str]]:
    """Category with the most keyword hits, plus the hits."""
    best: tuple[str | None, list[str]] = (None, [])
    for name, keywords in categories.items():
        hits = matched_keywords(text, keywords)
        if len(hits) > len(best[1]):
            best = (name, hits)
    return best


URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def count_links(text: str) -> int:
    return len(URL_RE.findall(text))


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].lower()
