"""Unit-of-measure normalisation and conversion.

Quotations arrive in whatever UOM the supplier prefers: a PR for 500 EA gets
quoted "per 100 pcs", "per box of 25", or "per kg". Nothing downstream can be
compared until every line is expressed in the requisition's UOM, so this module
is a hard dependency of both the technical and the commercial comparison.

Two conversion classes exist:

* **Dimensional** - within a physical dimension (kg <-> g, m <-> mm). Fixed
  factors, always available.
* **Material-specific** - across dimensions (BOX -> EA, KG -> M for wire).
  Requires an alternate-unit factor from the material master; refusing to guess
  here is deliberate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .errors import UnitConversionError


class Dimension(StrEnum):
    COUNT = "COUNT"
    MASS = "MASS"
    LENGTH = "LENGTH"
    AREA = "AREA"
    VOLUME = "VOLUME"
    TIME = "TIME"
    ENERGY = "ENERGY"
    PACKAGING = "PACKAGING"  # BOX/SET/PAC - needs a material-specific factor
    # Engineering dimensions. These never appear as an order unit, but they are
    # the units requirements are written in, and technical comparison has to
    # convert them ("min 16 bar" vs "232 psi offered").
    PRESSURE = "PRESSURE"
    TEMPERATURE = "TEMPERATURE"
    FORCE = "FORCE"
    TORQUE = "TORQUE"
    POWER = "POWER"
    VOLTAGE = "VOLTAGE"
    CURRENT = "CURRENT"
    FREQUENCY = "FREQUENCY"
    FLOW = "FLOW"
    ANGLE = "ANGLE"
    RATIO = "RATIO"


@dataclass(frozen=True, slots=True)
class UnitDefinition:
    code: str
    dimension: Dimension
    # Multiplier to the dimension's base unit (EA, KG, M, M2, L, H, KWH, BAR, K).
    to_base: Decimal
    description: str
    # Additive term applied *after* scaling, so that affine scales such as
    # Celsius and Fahrenheit convert correctly instead of being off by 273.15.
    offset: Decimal = Decimal(0)


# SAP-style ISO/commercial UOM codes seen in real material masters.
_UNITS: tuple[UnitDefinition, ...] = (
    # count
    UnitDefinition("EA", Dimension.COUNT, Decimal(1), "Each"),
    UnitDefinition("PC", Dimension.COUNT, Decimal(1), "Piece"),
    UnitDefinition("NO", Dimension.COUNT, Decimal(1), "Number"),
    UnitDefinition("DZ", Dimension.COUNT, Decimal(12), "Dozen"),
    UnitDefinition("C62", Dimension.COUNT, Decimal(1), "One (ISO)"),
    UnitDefinition("HUN", Dimension.COUNT, Decimal(100), "Hundred"),
    UnitDefinition("TSD", Dimension.COUNT, Decimal(1000), "Thousand"),
    # mass
    UnitDefinition("KG", Dimension.MASS, Decimal(1), "Kilogram"),
    UnitDefinition("G", Dimension.MASS, Decimal("0.001"), "Gram"),
    UnitDefinition("MG", Dimension.MASS, Decimal("0.000001"), "Milligram"),
    UnitDefinition("TO", Dimension.MASS, Decimal(1000), "Metric tonne"),
    UnitDefinition("TON", Dimension.MASS, Decimal(1000), "Metric tonne"),
    UnitDefinition("LB", Dimension.MASS, Decimal("0.45359237"), "Pound"),
    UnitDefinition("OZ", Dimension.MASS, Decimal("0.028349523125"), "Ounce"),
    # length
    UnitDefinition("M", Dimension.LENGTH, Decimal(1), "Metre"),
    UnitDefinition("MTR", Dimension.LENGTH, Decimal(1), "Metre"),
    UnitDefinition("MM", Dimension.LENGTH, Decimal("0.001"), "Millimetre"),
    UnitDefinition("UM", Dimension.LENGTH, Decimal("0.000001"), "Micrometre"),
    UnitDefinition("CM", Dimension.LENGTH, Decimal("0.01"), "Centimetre"),
    UnitDefinition("KM", Dimension.LENGTH, Decimal(1000), "Kilometre"),
    UnitDefinition("FT", Dimension.LENGTH, Decimal("0.3048"), "Foot"),
    UnitDefinition("IN", Dimension.LENGTH, Decimal("0.0254"), "Inch"),
    UnitDefinition("YD", Dimension.LENGTH, Decimal("0.9144"), "Yard"),
    # area
    UnitDefinition("M2", Dimension.AREA, Decimal(1), "Square metre"),
    UnitDefinition("CM2", Dimension.AREA, Decimal("0.0001"), "Square centimetre"),
    UnitDefinition("MM2", Dimension.AREA, Decimal("0.000001"), "Square millimetre"),
    UnitDefinition("FT2", Dimension.AREA, Decimal("0.09290304"), "Square foot"),
    # volume
    UnitDefinition("L", Dimension.VOLUME, Decimal(1), "Litre"),
    UnitDefinition("ML", Dimension.VOLUME, Decimal("0.001"), "Millilitre"),
    UnitDefinition("M3", Dimension.VOLUME, Decimal(1000), "Cubic metre"),
    UnitDefinition("GAL", Dimension.VOLUME, Decimal("3.785411784"), "US gallon"),
    # time
    UnitDefinition("H", Dimension.TIME, Decimal(1), "Hour"),
    UnitDefinition("HUR", Dimension.TIME, Decimal(1), "Hour"),
    UnitDefinition("MIN", Dimension.TIME, Decimal("0.016666666667"), "Minute"),
    UnitDefinition("DAY", Dimension.TIME, Decimal(24), "Day"),
    # Lead times and warranties are quoted in weeks, months and years, and are
    # compared against requirements written in a different one of those.
    UnitDefinition("WEEK", Dimension.TIME, Decimal(168), "Week"),
    UnitDefinition("MONTH", Dimension.TIME, Decimal(730), "Month (30.4 days)"),
    UnitDefinition("YEAR", Dimension.TIME, Decimal(8760), "Year"),
    # energy
    UnitDefinition("KWH", Dimension.ENERGY, Decimal(1), "Kilowatt hour"),
    UnitDefinition("MWH", Dimension.ENERGY, Decimal(1000), "Megawatt hour"),
    # packaging - base factor is meaningless without material context
    UnitDefinition("BOX", Dimension.PACKAGING, Decimal(1), "Box"),
    UnitDefinition("CAR", Dimension.PACKAGING, Decimal(1), "Carton"),
    UnitDefinition("PAC", Dimension.PACKAGING, Decimal(1), "Pack"),
    UnitDefinition("SET", Dimension.PACKAGING, Decimal(1), "Set"),
    UnitDefinition("ROL", Dimension.PACKAGING, Decimal(1), "Roll"),
    UnitDefinition("PAL", Dimension.PACKAGING, Decimal(1), "Pallet"),
    UnitDefinition("DR", Dimension.PACKAGING, Decimal(1), "Drum"),
    UnitDefinition("BAG", Dimension.PACKAGING, Decimal(1), "Bag"),
    UnitDefinition("CTN", Dimension.PACKAGING, Decimal(1), "Container"),
    # pressure (base: bar)
    UnitDefinition("BAR", Dimension.PRESSURE, Decimal(1), "Bar"),
    UnitDefinition("MBAR", Dimension.PRESSURE, Decimal("0.001"), "Millibar"),
    UnitDefinition("PA", Dimension.PRESSURE, Decimal("0.00001"), "Pascal"),
    UnitDefinition("KPA", Dimension.PRESSURE, Decimal("0.01"), "Kilopascal"),
    UnitDefinition("MPA", Dimension.PRESSURE, Decimal(10), "Megapascal"),
    UnitDefinition("PSI", Dimension.PRESSURE, Decimal("0.0689475729"), "Pounds per square inch"),
    UnitDefinition("ATM", Dimension.PRESSURE, Decimal("1.01325"), "Standard atmosphere"),
    # temperature (base: kelvin) - the only affine scales in the system
    UnitDefinition("K", Dimension.TEMPERATURE, Decimal(1), "Kelvin"),
    UnitDefinition("C", Dimension.TEMPERATURE, Decimal(1), "Degrees Celsius", Decimal("273.15")),
    UnitDefinition(
        "F", Dimension.TEMPERATURE, Decimal(5) / Decimal(9), "Degrees Fahrenheit",
        Decimal("255.372222222222"),
    ),
    # force / torque
    UnitDefinition("N", Dimension.FORCE, Decimal(1), "Newton"),
    UnitDefinition("KN", Dimension.FORCE, Decimal(1000), "Kilonewton"),
    UnitDefinition("LBF", Dimension.FORCE, Decimal("4.4482216153"), "Pound-force"),
    UnitDefinition("NM", Dimension.TORQUE, Decimal(1), "Newton metre"),
    UnitDefinition("KNM", Dimension.TORQUE, Decimal(1000), "Kilonewton metre"),
    UnitDefinition("LBFT", Dimension.TORQUE, Decimal("1.35581794833"), "Pound-foot"),
    # electrical
    UnitDefinition("W", Dimension.POWER, Decimal(1), "Watt"),
    UnitDefinition("KW", Dimension.POWER, Decimal(1000), "Kilowatt"),
    UnitDefinition("MW", Dimension.POWER, Decimal(1_000_000), "Megawatt"),
    UnitDefinition("HP", Dimension.POWER, Decimal("745.699872"), "Horsepower"),
    UnitDefinition("V", Dimension.VOLTAGE, Decimal(1), "Volt"),
    UnitDefinition("MV", Dimension.VOLTAGE, Decimal("0.001"), "Millivolt"),
    UnitDefinition("KV", Dimension.VOLTAGE, Decimal(1000), "Kilovolt"),
    UnitDefinition("A", Dimension.CURRENT, Decimal(1), "Ampere"),
    UnitDefinition("MA", Dimension.CURRENT, Decimal("0.001"), "Milliampere"),
    UnitDefinition("HZ", Dimension.FREQUENCY, Decimal(1), "Hertz"),
    UnitDefinition("KHZ", Dimension.FREQUENCY, Decimal(1000), "Kilohertz"),
    UnitDefinition("RPM", Dimension.FREQUENCY, Decimal(1) / Decimal(60), "Revolutions per minute"),
    # flow (base: litres per minute)
    UnitDefinition("LPM", Dimension.FLOW, Decimal(1), "Litres per minute"),
    UnitDefinition("GPM", Dimension.FLOW, Decimal("3.785411784"), "US gallons per minute"),
    UnitDefinition("M3H", Dimension.FLOW, Decimal(1000) / Decimal(60), "Cubic metres per hour"),
    UnitDefinition("CFM", Dimension.FLOW, Decimal("28.316846592"), "Cubic feet per minute"),
    # dimensionless
    UnitDefinition("PCT", Dimension.RATIO, Decimal(1), "Percent"),
    UnitDefinition("DEG", Dimension.ANGLE, Decimal(1), "Degrees of arc"),
)

UNIT_BY_CODE: dict[str, UnitDefinition] = {u.code: u for u in _UNITS}

# Free-text spellings suppliers actually write in quotations.
_ALIASES: dict[str, str] = {
    "EACH": "EA",
    "EACHES": "EA",
    "PCS": "PC",
    "PIECE": "PC",
    "PIECES": "PC",
    "UNIT": "EA",
    "UNITS": "EA",
    "NOS": "NO",
    "DOZEN": "DZ",
    "KGS": "KG",
    "KILOGRAM": "KG",
    "KILOGRAMS": "KG",
    "KILO": "KG",
    "GRAM": "G",
    "GRAMS": "G",
    "GMS": "G",
    "TONNE": "TO",
    "TONNES": "TO",
    "MT": "TO",
    "METRIC TON": "TO",
    "POUND": "LB",
    "POUNDS": "LB",
    "LBS": "LB",
    "METER": "M",
    "METERS": "M",
    "METRE": "M",
    "METRES": "M",
    "MTRS": "M",
    "MILLIMETER": "MM",
    "MILLIMETRE": "MM",
    "CENTIMETER": "CM",
    "FOOT": "FT",
    "FEET": "FT",
    "INCH": "IN",
    "INCHES": "IN",
    '"': "IN",
    "SQM": "M2",
    "SQ M": "M2",
    "SQ.M": "M2",
    "SQFT": "FT2",
    "LITER": "L",
    "LITERS": "L",
    "LITRE": "L",
    "LITRES": "L",
    "LTR": "L",
    "CUBIC METER": "M3",
    "CBM": "M3",
    "HOUR": "H",
    "HOURS": "H",
    "HRS": "H",
    "HR": "H",
    "DAYS": "DAY",
    "CALENDAR DAYS": "DAY",
    "WORKING DAYS": "DAY",
    "WEEKS": "WEEK",
    "WKS": "WEEK",
    "MONTHS": "MONTH",
    "MTH": "MONTH",
    "MTHS": "MONTH",
    "YEARS": "YEAR",
    "YRS": "YEAR",
    "YR": "YEAR",
    "BOXES": "BOX",
    "CARTON": "CAR",
    "CARTONS": "CAR",
    "PACK": "PAC",
    "PACKET": "PAC",
    "SETS": "SET",
    "ROLL": "ROL",
    "ROLLS": "ROL",
    "PALLET": "PAL",
    "PALLETS": "PAL",
    "DRUM": "DR",
    "DRUMS": "DR",
    "BAGS": "BAG",
    # engineering spellings
    "BARG": "BAR",
    "BARA": "BAR",
    "MILLIBAR": "MBAR",
    "PASCAL": "PA",
    "KILOPASCAL": "KPA",
    "MEGAPASCAL": "MPA",
    "N/MM2": "MPA",
    "N/MM²": "MPA",
    "LBF/IN2": "PSI",
    "°C": "C",
    "DEGC": "C",
    "DEG C": "C",
    "CELSIUS": "C",
    "°F": "F",
    "DEGF": "F",
    "DEG F": "F",
    "FAHRENHEIT": "F",
    "KELVIN": "K",
    "NEWTON": "N",
    "KILONEWTON": "KN",
    "NEWTON METRE": "NM",
    "NEWTON METER": "NM",
    "N M": "NM",
    "LB-FT": "LBFT",
    "FT-LB": "LBFT",
    "WATT": "W",
    "WATTS": "W",
    "KILOWATT": "KW",
    "VOLT": "V",
    "VOLTS": "V",
    "AMP": "A",
    "AMPS": "A",
    "AMPERE": "A",
    "HERTZ": "HZ",
    "REV/MIN": "RPM",
    "M3/H": "M3H",
    "M³/H": "M3H",
    "L/MIN": "LPM",
    "PERCENT": "PCT",
    "%": "PCT",
    "MICRON": "UM",
    "MICROMETRE": "UM",
    "MICROMETER": "UM",
    "µM": "UM",
    "DEGREE": "DEG",
    "DEGREES": "DEG",
}

BASE_UNIT_BY_DIMENSION: dict[Dimension, str] = {
    Dimension.COUNT: "EA",
    Dimension.MASS: "KG",
    Dimension.LENGTH: "M",
    Dimension.AREA: "M2",
    Dimension.VOLUME: "L",
    Dimension.TIME: "H",
    Dimension.ENERGY: "KWH",
    Dimension.PACKAGING: "EA",
}


def normalize_uom(raw: str) -> str:
    """Map a free-text unit onto a canonical code.

    Raises UnitConversionError rather than guessing, because a silently wrong
    unit corrupts every downstream price comparison.
    """
    if raw is None:
        raise UnitConversionError("UOM is required")
    # The micro sign (U+00B5) and Greek mu (U+03BC) both uppercase to Greek
    # capital Mu, which no alias table can match; fold them to "U" first.
    token = str(raw).strip()
    for micro in ("µ", "μ", "Μ"):
        token = token.replace(micro, "u")
    token = token.upper().replace(".", "").replace("_", " ")
    token = " ".join(token.split())
    if not token:
        raise UnitConversionError("UOM is required")
    if token in UNIT_BY_CODE:
        return token
    if token in _ALIASES:
        return _ALIASES[token]
    compact = token.replace(" ", "")
    if compact in UNIT_BY_CODE:
        return compact
    if compact in _ALIASES:
        return _ALIASES[compact]
    raise UnitConversionError(f"Unrecognised unit of measure: {raw!r}", raw=raw)


def try_normalize_uom(raw: str, default: str = "EA") -> str:
    try:
        return normalize_uom(raw)
    except UnitConversionError:
        return default


def normalize_engineering_unit(raw: str) -> str:
    """Canonicalise a unit found in a specification, preserving unknowns.

    Order units may be rejected when unrecognised - a wrong order unit corrupts
    a price. Engineering units may not: dropping "bar" from "minimum 16 bar"
    turns a checkable requirement into an unchecked number, which is worse than
    carrying a unit the converter does not understand. Unknown tokens are
    returned cleaned and uppercased so they still compare like-for-like.
    """
    token = (raw or "").strip()
    if not token:
        return ""
    try:
        return normalize_uom(token)
    except UnitConversionError:
        cleaned = "".join(c for c in token.upper() if c.isalnum() or c in "/%°²³")
        return cleaned[:16]


def dimension_of(uom: str) -> Dimension:
    return UNIT_BY_CODE[normalize_uom(uom)].dimension


@dataclass(frozen=True, slots=True)
class AlternateUnit:
    """Material-master alternate unit: `factor` base units per `alt_uom`.

    Example: material CBL-0042 sold on 100 m rolls -> AlternateUnit("ROL", 100, "M").
    """

    alt_uom: str
    factor: Decimal
    base_uom: str


class UnitConverter:
    """Converts quantities between units.

    `alternate_units` carries the material-specific bridges that make
    cross-dimension conversion legal (a box only equals 25 pieces because the
    material master says so).
    """

    def __init__(self, alternate_units: dict[str, AlternateUnit] | None = None) -> None:
        self._alternates: dict[str, AlternateUnit] = {}
        for code, alt in (alternate_units or {}).items():
            self._alternates[normalize_uom(code)] = AlternateUnit(
                normalize_uom(alt.alt_uom), Decimal(str(alt.factor)), normalize_uom(alt.base_uom)
            )

    def with_alternates(self, alternates: dict[str, AlternateUnit]) -> UnitConverter:
        merged = dict(self._alternates)
        for code, alt in alternates.items():
            merged[normalize_uom(code)] = alt
        return UnitConverter(merged)

    def factor(self, from_uom: str, to_uom: str) -> Decimal:
        """Multiplier that turns a quantity in `from_uom` into `to_uom`.

        Only valid for ratio scales. Temperature is affine, so a single
        multiplier cannot express it and this raises rather than returning a
        number that is wrong by 273.15 - use `convert` for those.
        """
        src = normalize_uom(from_uom)
        dst = normalize_uom(to_uom)
        if src == dst:
            return Decimal(1)

        src_def, dst_def = UNIT_BY_CODE[src], UNIT_BY_CODE[dst]

        if src_def.offset or dst_def.offset:
            raise UnitConversionError(
                f"{src}->{dst} is an affine conversion and has no single scaling factor; "
                f"use convert() instead",
                from_uom=src,
                to_uom=dst,
            )

        # Same physical dimension: pure ratio of base factors.
        if (
            src_def.dimension == dst_def.dimension
            and src_def.dimension is not Dimension.PACKAGING
        ):
            return src_def.to_base / dst_def.to_base

        # Cross-dimension or packaging: needs a material-master bridge.
        bridged = self._bridge(src, dst)
        if bridged is not None:
            return bridged

        raise UnitConversionError(
            f"No conversion from {src} to {dst}; a material-master alternate unit is required",
            from_uom=src,
            to_uom=dst,
        )

    def _bridge(self, src: str, dst: str) -> Decimal | None:
        # Forward: src is an alternate unit expressed in some base unit.
        alt = self._alternates.get(src)
        if alt is not None:
            try:
                return alt.factor * self.factor(alt.base_uom, dst)
            except UnitConversionError:
                pass
        # Reverse: dst is the alternate unit, so invert.
        alt = self._alternates.get(dst)
        if alt is not None and alt.factor != 0:
            try:
                return self.factor(src, alt.base_uom) / alt.factor
            except UnitConversionError:
                pass
        return None

    def convert(self, quantity: Decimal | float | str, from_uom: str, to_uom: str) -> Decimal:
        """Convert a quantity, handling both ratio and affine scales."""
        src = normalize_uom(from_uom)
        dst = normalize_uom(to_uom)
        value = Decimal(str(quantity))
        if src == dst:
            return value

        src_def, dst_def = UNIT_BY_CODE[src], UNIT_BY_CODE[dst]
        if src_def.offset or dst_def.offset:
            if src_def.dimension != dst_def.dimension:
                raise UnitConversionError(
                    f"Cannot convert {src} to {dst}: different dimensions",
                    from_uom=src,
                    to_uom=dst,
                )
            # value -> base -> target, carrying the additive term through.
            in_base = value * src_def.to_base + src_def.offset
            return (in_base - dst_def.offset) / dst_def.to_base

        return value * self.factor(src, dst)

    def convert_price(
        self,
        unit_price: Decimal | float | str,
        *,
        from_uom: str,
        to_uom: str,
        price_per_quantity: Decimal | float | str = 1,
    ) -> Decimal:
        """Convert a unit price, inverting the quantity factor.

        A price of 250 USD per 100 PC becomes 2.50 USD per PC, and 12 USD/KG
        becomes 0.012 USD/G. `price_per_quantity` handles "per 100 pcs" pricing.
        """
        per = Decimal(str(price_per_quantity))
        if per <= 0:
            raise UnitConversionError("price_per_quantity must be positive", value=str(per))
        qty_factor = self.factor(from_uom, to_uom)
        if qty_factor == 0:
            raise UnitConversionError("Degenerate unit conversion factor")
        return Decimal(str(unit_price)) / per / qty_factor

    def can_convert(self, from_uom: str, to_uom: str) -> bool:
        try:
            self.factor(from_uom, to_uom)
            return True
        except UnitConversionError:
            return False


DEFAULT_CONVERTER = UnitConverter()
