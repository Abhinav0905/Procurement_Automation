"""Money, units and the commercial comparison maths.

These are the calculations that decide who wins, so they are tested against
worked examples rather than only for self-consistency.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from procureguard.domain.errors import CurrencyConversionError, UnitConversionError, ValidationError
from procureguard.domain.money import FxRate, FxRateTable, Money, PaymentTerms
from procureguard.domain.units import AlternateUnit, UnitConverter, normalize_uom

# ── Money ────────────────────────────────────────────────────────────────────

def test_money_refuses_to_mix_currencies():
    with pytest.raises(CurrencyConversionError):
        Money(Decimal(10), "EUR") + Money(Decimal(10), "USD")


def test_money_parses_us_and_european_formats():
    assert Money.parse("$1,234.56").amount == Decimal("1234.56")
    assert Money.parse("EUR 1.234,56").amount == Decimal("1234.56")
    assert Money.parse("12 500 USD").amount == Decimal("12500")
    assert Money.parse("1,234", "USD").amount == Decimal("1234")
    assert Money.parse("1,23", "EUR").amount == Decimal("1.23")


def test_money_quantises_to_currency_exponent():
    assert Money(Decimal("10.005"), "USD").quantize().amount == Decimal("10.01")
    assert Money(Decimal("10.5"), "JPY").quantize().amount == Decimal("11")


def test_money_rejects_bad_currency():
    with pytest.raises(ValidationError):
        Money(Decimal(1), "EURO")


# ── FX ───────────────────────────────────────────────────────────────────────

def _fx() -> FxRateTable:
    table = FxRateTable("USD")
    table.add(FxRate("USD", "EUR", Decimal("0.90"), date(2026, 1, 1)))
    table.add(FxRate("USD", "EUR", Decimal("0.95"), date(2026, 6, 1)))
    table.add(FxRate("USD", "JPY", Decimal("150"), date(2026, 1, 1)))
    return table


def test_fx_uses_the_rate_in_force_on_the_value_date():
    table = _fx()
    assert table.rate("USD", "EUR", date(2026, 3, 1)) == Decimal("0.90")
    assert table.rate("USD", "EUR", date(2026, 7, 1)) == Decimal("0.95")


def test_fx_inverts_and_triangulates():
    table = _fx()
    assert table.rate("EUR", "USD", date(2026, 3, 1)) == Decimal(1) / Decimal("0.90")
    # EUR -> JPY has no direct rate; it goes through the USD pivot.
    assert table.rate("EUR", "JPY", date(2026, 3, 1)) == Decimal(150) / Decimal("0.90")


def test_fx_missing_pair_raises_rather_than_assuming_parity():
    with pytest.raises(CurrencyConversionError):
        FxRateTable("USD").rate("USD", "BRL", date(2026, 1, 1))


# ── units ────────────────────────────────────────────────────────────────────

def test_supplier_unit_spellings_normalise():
    assert normalize_uom("pcs") == "PC"
    assert normalize_uom("Kilograms") == "KG"
    assert normalize_uom("µm") == "UM"
    assert normalize_uom("N/mm2") == "MPA"


def test_unknown_order_unit_raises():
    with pytest.raises(UnitConversionError):
        normalize_uom("sackfuls")


def test_dimensional_conversion():
    converter = UnitConverter()
    assert converter.convert(1, "KG", "G") == Decimal(1000)
    assert converter.convert(Decimal("2.5"), "M", "MM") == Decimal(2500)


def test_temperature_is_affine_not_a_ratio():
    converter = UnitConverter()
    assert round(converter.convert(200, "C", "F"), 2) == Decimal("392.00")
    assert round(converter.convert(32, "F", "C"), 4) == Decimal("0.0000")
    assert round(converter.convert(-20, "C", "K"), 2) == Decimal("253.15")
    # A single multiplier cannot express an affine scale, so factor() refuses.
    with pytest.raises(UnitConversionError):
        converter.factor("C", "F")


def test_packaging_needs_a_material_master_factor():
    plain = UnitConverter()
    with pytest.raises(UnitConversionError):
        plain.convert(2, "BOX", "EA")

    bridged = UnitConverter({"BOX": AlternateUnit("BOX", Decimal(25), "EA")})
    assert bridged.convert(2, "BOX", "EA") == Decimal(50)


def test_price_conversion_inverts_the_quantity_factor():
    converter = UnitConverter()
    # 250 per 100 pieces is 2.50 each.
    assert converter.convert_price(250, from_uom="PC", to_uom="PC", price_per_quantity=100) == Decimal("2.5")
    # 12 per kg is 0.012 per gram.
    assert converter.convert_price(12, from_uom="KG", to_uom="G") == Decimal("0.012")


# ── payment terms ────────────────────────────────────────────────────────────

def test_payment_terms_parsing():
    assert PaymentTerms.parse("2/10 net 30").discount_pct == Decimal(2)
    assert PaymentTerms.parse("2/10 net 30").net_days == 30
    assert PaymentTerms.parse("NET 60").net_days == 60
    assert PaymentTerms.parse("50% advance").requires_advance
    assert PaymentTerms.parse("LC at sight").requires_lc
    assert PaymentTerms.parse("").net_days == 30


def test_longer_credit_is_worth_money_and_prepayment_costs_money():
    gross = Money(Decimal(100_000), "USD")
    daily = Decimal("0.09") / Decimal(365)

    net30 = PaymentTerms.parse("NET 30").present_value(gross, daily)
    net90 = PaymentTerms.parse("NET 90").present_value(gross, daily)
    advance = PaymentTerms.parse("100% advance").present_value(gross, daily)

    assert net90.amount < net30.amount < gross.amount
    assert advance.amount > gross.amount


def test_early_payment_discount_is_captured():
    gross = Money(Decimal(100_000), "USD")
    daily = Decimal("0.09") / Decimal(365)
    discounted = PaymentTerms.parse("2/10 net 30").present_value(gross, daily)
    # Taking a 2% discount for paying 20 days early beats the cost of capital.
    assert discounted.amount < Decimal(99_000)


# ── incoterm cost responsibility ─────────────────────────────────────────────

def test_incoterm_matrix_assigns_buyer_costs_correctly():
    from procureguard.application.commercial_normalization import INCOTERM_BUYER_COSTS

    exw = INCOTERM_BUYER_COSTS["EXW"]
    cif = INCOTERM_BUYER_COSTS["CIF"]
    ddp = INCOTERM_BUYER_COSTS["DDP"]

    # EXW leaves everything to the buyer; DDP leaves nothing.
    assert "main_freight" in exw and "duty" in exw
    assert not ddp
    # CIF includes freight and insurance but not import duty.
    assert "main_freight" not in cif and "insurance" not in cif
    assert "duty" in cif


def test_freight_split_is_exhaustive():
    from procureguard.application.commercial_normalization import FREIGHT_SPLIT

    assert sum(FREIGHT_SPLIT.values()) == Decimal(1)


# ── ranking maths ────────────────────────────────────────────────────────────

def test_value_score_balances_cost_against_technical_merit():
    from procureguard.application.bid_ranking import (
        COST_WEIGHT,
        TECHNICAL_WEIGHT,
        BidRankingService,
        RankedBid,
        RankingResult,
    )

    cheap_poor = RankedBid(
        vendor_id="A", vendor_name="A", quotation_id="", position=1, position_label="L1",
        total_base=Decimal(100), landed_cost_base=Decimal(100), tco_base=Decimal(100),
        delta_vs_l1_base=Decimal(0), delta_vs_l1_pct=Decimal(0), delta_vs_benchmark_pct=None,
        technical_score=Decimal(60), weighted_value_score=None, technically_qualified=True,
        lines_covered=1, lines_total=1, partial_offer=False,
    )
    dearer_better = RankedBid(
        vendor_id="B", vendor_name="B", quotation_id="", position=2, position_label="L2",
        total_base=Decimal(104), landed_cost_base=Decimal(104), tco_base=Decimal(104),
        delta_vs_l1_base=Decimal(4), delta_vs_l1_pct=Decimal(4), delta_vs_benchmark_pct=None,
        technical_score=Decimal(98), weighted_value_score=None, technically_qualified=True,
        lines_covered=1, lines_total=1, partial_offer=False,
    )
    result = RankingResult(case_id="C", ranking_run_id="R", negotiation_round=0, basis="TCO", base_currency="USD")
    BidRankingService._compute_value_scores([cheap_poor, dearer_better], result)

    assert Decimal(100) == COST_WEIGHT + TECHNICAL_WEIGHT
    # 4% dearer but 38 points better technically wins on value.
    assert result.value_order[0] == "B"
    assert dearer_better.weighted_value_score > cheap_poor.weighted_value_score
