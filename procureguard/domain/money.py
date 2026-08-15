"""Money, currency and time-value arithmetic.

Rules enforced here:

* Money is always Decimal. Float money is a defect.
* Arithmetic between different currencies raises rather than coercing.
* Conversion needs an explicit as-of date; a quote received in March is not
  converted at today's rate.
* Payment terms have a real cost of capital, so "2% net 10 vs net 90" is
  resolved into a comparable present value instead of being ignored.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .errors import CurrencyConversionError, ValidationError

# ISO 4217 minor units for the currencies this deployment trades in.
CURRENCY_EXPONENT: dict[str, int] = {
    "USD": 2, "EUR": 2, "GBP": 2, "CHF": 2, "SEK": 2, "NOK": 2, "DKK": 2,
    "PLN": 2, "CZK": 2, "TRY": 2, "INR": 2, "CNY": 2, "SGD": 2, "AUD": 2,
    "CAD": 2, "MXN": 2, "BRL": 2, "ZAR": 2, "AED": 2, "SAR": 2, "THB": 2,
    "MYR": 2, "PHP": 2, "IDR": 2, "HKD": 2, "NZD": 2, "ILS": 2,
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0,
    "BHD": 3, "KWD": 3, "OMR": 3, "TND": 3,
}
DEFAULT_EXPONENT = 2

CURRENCY_SYMBOLS: dict[str, str] = {
    "$": "USD", "US$": "USD", "USD$": "USD",
    "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW",
    "CHF": "CHF", "SGD$": "SGD", "S$": "SGD", "A$": "AUD", "C$": "CAD",
    "RM": "MYR", "฿": "THB", "₺": "TRY", "R$": "BRL", "kr": "SEK",
}


def currency_exponent(currency: str) -> int:
    return CURRENCY_EXPONENT.get(currency.upper(), DEFAULT_EXPONENT)


def quantum_for(currency: str) -> Decimal:
    return Decimal(1).scaleb(-currency_exponent(currency))


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact monetary amount in a single currency."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", str(self.currency).strip().upper())
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise ValidationError(f"Invalid currency code: {self.currency!r}")
        try:
            object.__setattr__(self, "amount", Decimal(str(self.amount)))
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise ValidationError(f"Invalid monetary amount: {self.amount!r}") from exc

    # ------------------------------------------------------------- constructors
    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(Decimal(0), currency)

    @classmethod
    def of(cls, amount: Decimal | float | int | str, currency: str) -> Money:
        return cls(Decimal(str(amount)), currency)

    @classmethod
    def parse(cls, text: str, default_currency: str | None = None) -> Money:
        """Parse supplier-written amounts: '$1,234.56', 'EUR 1.234,56', '12 500 USD'."""
        raw = str(text).strip()
        if not raw:
            raise ValidationError("Empty monetary value")

        currency = None
        upper = raw.upper()
        iso = re.search(r"\b([A-Z]{3})\b", upper)
        if iso and iso.group(1) in CURRENCY_EXPONENT:
            currency = iso.group(1)
            upper = upper.replace(iso.group(1), " ")
        else:
            for symbol, code in sorted(
                CURRENCY_SYMBOLS.items(), key=lambda kv: -len(kv[0])
            ):
                if symbol.upper() in upper:
                    currency = code
                    upper = upper.replace(symbol.upper(), " ")
                    break
        currency = currency or (default_currency or "").upper()
        if not currency:
            raise ValidationError(f"Cannot determine currency from {text!r}")

        numeric = re.sub(r"[^0-9,.\-]", "", upper)
        if not numeric:
            raise ValidationError(f"Cannot determine amount from {text!r}")
        numeric = _normalize_decimal_separators(numeric)
        try:
            return cls(Decimal(numeric), currency)
        except InvalidOperation as exc:
            raise ValidationError(f"Cannot parse amount from {text!r}") from exc

    # ---------------------------------------------------------------- arithmetic
    def _assert_same(self, other: Money) -> None:
        if self.currency != other.currency:
            raise CurrencyConversionError(
                f"Cannot combine {self.currency} with {other.currency} without an FX rate",
                left=self.currency,
                right=other.currency,
            )

    def __add__(self, other: Money) -> Money:
        self._assert_same(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._assert_same(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: Decimal | int | float | str) -> Money:
        return Money(self.amount * Decimal(str(factor)), self.currency)

    __rmul__ = __mul__

    def __truediv__(self, divisor: Decimal | int | float | str) -> Money:
        d = Decimal(str(divisor))
        if d == 0:
            raise ValidationError("Division of money by zero")
        return Money(self.amount / d, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._assert_same(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._assert_same(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._assert_same(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._assert_same(other)
        return self.amount >= other.amount

    # ------------------------------------------------------------------ helpers
    def quantize(self) -> Money:
        return Money(
            self.amount.quantize(quantum_for(self.currency), rounding=ROUND_HALF_UP),
            self.currency,
        )

    def round_to(self, places: int) -> Money:
        return Money(
            self.amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP),
            self.currency,
        )

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    def pct_of(self, base: Money) -> Decimal:
        self._assert_same(base)
        if base.amount == 0:
            raise ValidationError("Percentage of zero base is undefined")
        return (self.amount / base.amount * Decimal(100)).quantize(Decimal("0.0001"))

    def apply_pct(self, pct: Decimal | float | str) -> Money:
        return Money(self.amount * (Decimal(1) + Decimal(str(pct)) / Decimal(100)), self.currency)

    def to_dict(self) -> dict[str, str]:
        return {"amount": str(self.quantize().amount), "currency": self.currency}

    def __str__(self) -> str:
        return f"{self.quantize().amount} {self.currency}"

    def __repr__(self) -> str:
        return f"Money({self.amount!s}, {self.currency!r})"


def _normalize_decimal_separators(numeric: str) -> str:
    """Disambiguate '1.234,56' (EU) from '1,234.56' (US) from '1234.56'."""
    has_comma, has_dot = "," in numeric, "." in numeric
    if has_comma and has_dot:
        # Whichever separator appears last is the decimal point.
        if numeric.rfind(",") > numeric.rfind("."):
            return numeric.replace(".", "").replace(",", ".")
        return numeric.replace(",", "")
    if has_comma:
        tail = numeric.rsplit(",", 1)[1]
        # "1,234" is thousands; "1,23" is decimal.
        return numeric.replace(",", "") if len(tail) == 3 else numeric.replace(",", ".")
    return numeric


def money_sum(items: Iterable[Money], currency: str) -> Money:
    total = Money.zero(currency)
    for item in items:
        total = total + item
    return total


@dataclass(frozen=True, slots=True)
class FxRate:
    base: str
    quote: str
    rate: Decimal  # 1 base = `rate` quote
    as_of: date
    source: str = "INTERNAL"


class FxRateTable:
    """As-of-date FX lookups with triangulation through the base currency.

    Rates are picked as "the latest published rate on or before the value date",
    which is the behaviour auditors expect when they re-derive a bid comparison.
    """

    def __init__(self, base_currency: str = "USD") -> None:
        self.base_currency = base_currency.upper()
        self._rates: dict[tuple[str, str], list[FxRate]] = {}

    def add(self, rate: FxRate) -> None:
        key = (rate.base.upper(), rate.quote.upper())
        bucket = self._rates.setdefault(key, [])
        bucket.append(rate)
        bucket.sort(key=lambda r: r.as_of)

    def add_many(self, rates: Iterable[FxRate]) -> None:
        for rate in rates:
            self.add(rate)

    def _direct(self, base: str, quote: str, as_of: date) -> Decimal | None:
        bucket = self._rates.get((base, quote))
        if not bucket:
            return None
        candidates = [r for r in bucket if r.as_of <= as_of]
        if candidates:
            return candidates[-1].rate
        # Before the first published rate: use the earliest known, flagged by caller.
        return bucket[0].rate

    def rate(self, base: str, quote: str, as_of: date | datetime | None = None) -> Decimal:
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return Decimal(1)
        if as_of is None:
            as_of = date.today()
        if isinstance(as_of, datetime):
            as_of = as_of.date()

        direct = self._direct(base, quote, as_of)
        if direct is not None:
            return direct

        inverse = self._direct(quote, base, as_of)
        if inverse is not None and inverse != 0:
            return Decimal(1) / inverse

        # Triangulate: base -> pivot -> quote.
        pivot = self.base_currency
        if base != pivot and quote != pivot:
            leg_a = self._direct(pivot, base, as_of)
            leg_b = self._direct(pivot, quote, as_of)
            if leg_a and leg_b and leg_a != 0:
                return leg_b / leg_a

        raise CurrencyConversionError(
            f"No FX rate available for {base}->{quote} as of {as_of}",
            base=base,
            quote=quote,
            as_of=str(as_of),
        )

    def convert(
        self, money: Money, to_currency: str, as_of: date | datetime | None = None
    ) -> Money:
        to_currency = to_currency.upper()
        if money.currency == to_currency:
            return money
        return Money(
            money.amount * self.rate(money.currency, to_currency, as_of), to_currency
        )

    def known_pairs(self) -> list[tuple[str, str]]:
        return sorted(self._rates.keys())


# --------------------------------------------------------------- payment terms

_PAYMENT_TERM_PATTERNS = (
    # "2/10 net 30", "2% 10 days, net 30"
    (
        re.compile(
            r"(?P<disc>\d+(?:\.\d+)?)\s*%?\s*/?\s*(?P<ddays>\d+)\s*(?:days?)?[\s,]*net\s*(?P<net>\d+)",
            re.I,
        ),
        "discount",
    ),
    (re.compile(r"net\s*(?P<net>\d+)", re.I), "net"),
    (re.compile(r"(?P<net>\d+)\s*days?\s*(?:from|after)?", re.I), "net"),
    (re.compile(r"\b(?:cad|cash\s+against\s+documents)\b", re.I), "cad"),
    (re.compile(r"\b(?:cia|advance|prepay(?:ment)?|pia)\b", re.I), "advance"),
    (re.compile(r"\b(?:lc|letter\s+of\s+credit)\b", re.I), "lc"),
    (re.compile(r"\b(?:cod|cash\s+on\s+delivery)\b", re.I), "cod"),
)


@dataclass(frozen=True, slots=True)
class PaymentTerms:
    """Parsed payment terms with the data needed to price them."""

    raw: str
    net_days: int
    discount_pct: Decimal = Decimal(0)
    discount_days: int = 0
    requires_advance: bool = False
    requires_lc: bool = False

    @classmethod
    def parse(cls, text: str | None, default_net_days: int = 30) -> PaymentTerms:
        raw = (text or "").strip()
        if not raw:
            return cls(raw="", net_days=default_net_days)
        for pattern, kind in _PAYMENT_TERM_PATTERNS:
            match = pattern.search(raw)
            if not match:
                continue
            if kind == "discount":
                return cls(
                    raw=raw,
                    net_days=int(match.group("net")),
                    discount_pct=Decimal(match.group("disc")),
                    discount_days=int(match.group("ddays")),
                )
            if kind == "net":
                return cls(raw=raw, net_days=int(match.group("net")))
            if kind == "advance":
                return cls(raw=raw, net_days=0, requires_advance=True)
            if kind == "lc":
                return cls(raw=raw, net_days=default_net_days, requires_lc=True)
            if kind in ("cod", "cad"):
                return cls(raw=raw, net_days=0)
        return cls(raw=raw, net_days=default_net_days)

    def effective_days(self) -> int:
        """Days of credit actually taken, assuming the discount is captured."""
        if self.discount_pct > 0 and self.discount_days > 0:
            return self.discount_days
        return self.net_days

    def present_value(self, gross: Money, daily_rate: Decimal | float) -> Money:
        """Discount an invoice to the delivery date.

        Longer credit is genuinely worth money; an advance payment genuinely
        costs money. Both fall out of the same formula, with negative days for
        prepayment.
        """
        rate = Decimal(str(daily_rate))
        payable = gross
        if self.discount_pct > 0 and self.discount_days > 0:
            payable = gross * (Decimal(1) - self.discount_pct / Decimal(100))
        days = Decimal(self.effective_days())
        if self.requires_advance:
            days = Decimal(-abs(int(self.net_days) or 30))
        discount_factor = Decimal(1) / ((Decimal(1) + rate) ** days)
        return Money(payable.amount * discount_factor, payable.currency)

    def to_dict(self) -> dict[str, object]:
        return {
            "raw": self.raw,
            "net_days": self.net_days,
            "discount_pct": str(self.discount_pct),
            "discount_days": self.discount_days,
            "requires_advance": self.requires_advance,
            "requires_lc": self.requires_lc,
            "effective_days": self.effective_days(),
        }
