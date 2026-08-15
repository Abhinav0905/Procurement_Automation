"""Reference data for the synthetic enterprise.

A plausible mid-size industrial manufacturer: five plants, a real material
taxonomy across mechanical, electrical, hydraulic, fasteners, consumables and
services, and a supplier base with the geographic and performance spread a real
one has. The point is that the generated history has to *behave* like real
history - inflation, seasonality, supplier churn, quality problems, currency
mixes - or the analytics built on top of it are meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

TENANT_ID = "ACME-MFG"
BASE_CURRENCY = "USD"


@dataclass(frozen=True, slots=True)
class PlantSpec:
    code: str
    name: str
    city: str
    country: str
    currency: str
    timezone: str
    purchasing_org: str


PLANTS: tuple[PlantSpec, ...] = (
    PlantSpec("1000", "Detroit Assembly", "Detroit", "US", "USD", "America/Detroit", "1000"),
    PlantSpec("1100", "Monterrey Components", "Monterrey", "MX", "MXN", "America/Monterrey", "1000"),
    PlantSpec("2000", "Stuttgart Precision", "Stuttgart", "DE", "EUR", "Europe/Berlin", "2000"),
    PlantSpec("2100", "Brno Machining", "Brno", "CZ", "CZK", "Europe/Prague", "2000"),
    PlantSpec("3000", "Pune Fabrication", "Pune", "IN", "INR", "Asia/Kolkata", "3000"),
)


@dataclass(frozen=True, slots=True)
class MaterialGroupSpec:
    code: str
    # Short, explicitly unique material-number prefix. Deriving this from `code`
    # is what collided MG-ELEC with MG-ELECTRONIC, so it is stated outright.
    prefix: str
    text: str
    material_type: str
    base_uom: str
    # Log-uniform price band in base currency, per base unit.
    price_low: Decimal
    price_high: Decimal
    weight_kg_low: Decimal
    weight_kg_high: Decimal
    lead_time_days: tuple[int, int]
    quality_critical: bool
    hazardous: bool = False
    descriptors: tuple[str, ...] = ()
    materials_of_construction: tuple[str, ...] = ()
    attribute_ranges: dict[str, tuple[float, float, str]] = field(default_factory=dict)


MATERIAL_GROUPS: tuple[MaterialGroupSpec, ...] = (
    MaterialGroupSpec(
        code="MG-VALVE", prefix="VAL", text="Valves and flow control", material_type="ROH", base_uom="EA",
        price_low=Decimal(45), price_high=Decimal(4200),
        weight_kg_low=Decimal("1.5"), weight_kg_high=Decimal(85),
        lead_time_days=(21, 90), quality_critical=True,
        descriptors=("Gate valve", "Ball valve", "Globe valve", "Check valve", "Butterfly valve",
                     "Needle valve", "Pressure relief valve", "Solenoid valve"),
        materials_of_construction=("ASTM A216 WCB", "SS 316L", "SS 304", "Ductile iron",
                                   "Bronze C83600", "Duplex 2205"),
        attribute_ranges={
            "nominal_diameter_mm": (15, 300, "MM"),
            "pressure_rating_bar": (10, 250, "BAR"),
            "temperature_max_c": (80, 450, "C"),
        },
    ),
    MaterialGroupSpec(
        code="MG-BEARING", prefix="BRG", text="Bearings and bushings", material_type="ROH", base_uom="EA",
        price_low=Decimal("2.4"), price_high=Decimal(880),
        weight_kg_low=Decimal("0.02"), weight_kg_high=Decimal(12),
        lead_time_days=(14, 60), quality_critical=True,
        descriptors=("Deep groove ball bearing", "Tapered roller bearing", "Spherical roller bearing",
                     "Needle roller bearing", "Thrust bearing", "Linear bearing", "Bronze bushing"),
        materials_of_construction=("Chrome steel 52100", "SS 440C", "Ceramic hybrid", "Bronze"),
        attribute_ranges={
            "bore_diameter_mm": (8, 220, "MM"),
            "dynamic_load_rating_kn": (2, 480, "KN"),
            "max_speed_rpm": (1200, 24000, "RPM"),
        },
    ),
    MaterialGroupSpec(
        code="MG-HYD", prefix="HYD", text="Hydraulics and pneumatics", material_type="ROH", base_uom="EA",
        price_low=Decimal(65), price_high=Decimal(12500),
        weight_kg_low=Decimal("0.8"), weight_kg_high=Decimal(240),
        lead_time_days=(28, 120), quality_critical=True,
        descriptors=("Hydraulic pump", "Hydraulic cylinder", "Directional control valve",
                     "Pneumatic actuator", "Accumulator", "Hydraulic motor", "Filter assembly"),
        materials_of_construction=("Cast iron", "Aluminium 6061", "SS 316", "Carbon steel"),
        attribute_ranges={
            "working_pressure_bar": (60, 420, "BAR"),
            "flow_rate_lpm": (5, 600, "LPM"),
            "power_kw": (1, 250, "KW"),
        },
    ),
    MaterialGroupSpec(
        code="MG-ELEC", prefix="ELC", text="Electrical components", material_type="ROH", base_uom="EA",
        price_low=Decimal("1.2"), price_high=Decimal(9800),
        weight_kg_low=Decimal("0.01"), weight_kg_high=Decimal(180),
        lead_time_days=(10, 180), quality_critical=True,
        descriptors=("AC induction motor", "Variable frequency drive", "Contactor", "Circuit breaker",
                     "Terminal block", "Proximity sensor", "PLC input module", "Encoder",
                     "Cable gland", "Control transformer"),
        materials_of_construction=("Copper", "Aluminium", "Thermoplastic", "Steel"),
        attribute_ranges={
            "rated_voltage_v": (24, 690, "V"),
            "rated_current_a": (1, 630, "A"),
            "power_kw": (0.1, 315, "KW"),
        },
    ),
    MaterialGroupSpec(
        code="MG-FAST", prefix="FST", text="Fasteners", material_type="ROH", base_uom="EA",
        price_low=Decimal("0.04"), price_high=Decimal("18"),
        weight_kg_low=Decimal("0.002"), weight_kg_high=Decimal("1.4"),
        lead_time_days=(7, 35), quality_critical=False,
        descriptors=("Hex head bolt", "Socket head cap screw", "Hex nut", "Flat washer",
                     "Spring washer", "Threaded rod", "Dowel pin", "Retaining ring", "Rivet"),
        materials_of_construction=("Steel 8.8", "Steel 10.9", "SS A2-70", "SS A4-80", "Brass"),
        attribute_ranges={
            "thread_diameter_mm": (3, 42, "MM"),
            "length_mm": (8, 300, "MM"),
            "tensile_strength_mpa": (400, 1200, "MPA"),
        },
    ),
    MaterialGroupSpec(
        code="MG-SEAL", prefix="SEL", text="Seals and gaskets", material_type="ROH", base_uom="EA",
        price_low=Decimal("0.35"), price_high=Decimal(420),
        weight_kg_low=Decimal("0.005"), weight_kg_high=Decimal("6"),
        lead_time_days=(10, 45), quality_critical=True,
        descriptors=("O-ring", "Rotary shaft seal", "Spiral wound gasket", "Sheet gasket",
                     "Mechanical seal", "V-ring seal", "Wiper seal"),
        materials_of_construction=("NBR", "Viton FKM", "EPDM", "PTFE", "Graphite", "Silicone"),
        attribute_ranges={
            "inner_diameter_mm": (4, 500, "MM"),
            "temperature_max_c": (80, 260, "C"),
            "pressure_rating_bar": (5, 120, "BAR"),
        },
    ),
    MaterialGroupSpec(
        code="MG-RAW", prefix="RAW", text="Raw material and stock", material_type="ROH", base_uom="KG",
        price_low=Decimal("1.1"), price_high=Decimal(48),
        weight_kg_low=Decimal(1), weight_kg_high=Decimal(1),
        lead_time_days=(14, 70), quality_critical=True,
        descriptors=("Round bar", "Flat bar", "Hexagonal bar", "Steel plate", "Seamless tube",
                     "Angle section", "Sheet coil"),
        materials_of_construction=("S355JR", "AISI 4140", "SS 316L", "AISI 1045",
                                   "Aluminium 7075", "Brass CuZn39Pb3"),
        attribute_ranges={
            "diameter_mm": (6, 400, "MM"),
            "yield_strength_mpa": (235, 900, "MPA"),
        },
    ),
    MaterialGroupSpec(
        code="MG-CHEM", prefix="CHM", text="Chemicals and lubricants", material_type="ROH", base_uom="L",
        price_low=Decimal("2.8"), price_high=Decimal(190),
        weight_kg_low=Decimal("0.9"), weight_kg_high=Decimal("1.3"),
        lead_time_days=(7, 30), quality_critical=False, hazardous=True,
        descriptors=("Hydraulic oil", "Cutting fluid", "Degreaser", "Anti-seize compound",
                     "Thread locker", "Corrosion inhibitor", "Gear oil"),
        materials_of_construction=("Mineral oil", "Synthetic ester", "Water-miscible", "Solvent based"),
        attribute_ranges={
            "viscosity_cst_40c": (10, 460, ""),
            "flash_point_c": (60, 260, "C"),
        },
    ),
    MaterialGroupSpec(
        code="MG-TOOL", prefix="TOL", text="Tooling and consumables", material_type="HIBE", base_uom="EA",
        price_low=Decimal("3.2"), price_high=Decimal(1800),
        weight_kg_low=Decimal("0.01"), weight_kg_high=Decimal(24),
        lead_time_days=(7, 42), quality_critical=False,
        descriptors=("Carbide insert", "End mill", "Drill bit", "Reamer", "Grinding wheel",
                     "Tool holder", "Collet", "Saw blade"),
        materials_of_construction=("Carbide", "HSS", "PCD", "Ceramic"),
        attribute_ranges={
            "cutting_diameter_mm": (1, 125, "MM"),
            "hardness_hrc": (45, 92, "HRC"),
        },
    ),
    MaterialGroupSpec(
        code="MG-PACK", prefix="PCK", text="Packaging", material_type="VERP", base_uom="EA",
        price_low=Decimal("0.18"), price_high=Decimal(78),
        weight_kg_low=Decimal("0.05"), weight_kg_high=Decimal(28),
        lead_time_days=(5, 25), quality_critical=False,
        descriptors=("Corrugated carton", "Wooden pallet", "Stretch film", "Edge protector",
                     "Plastic crate", "VCI bag", "Strapping band"),
        materials_of_construction=("Kraft board", "Pine", "LDPE", "HDPE"),
        attribute_ranges={"load_capacity_kg": (5, 1500, "KG")},
    ),
    MaterialGroupSpec(
        code="MG-ELECTRONIC", prefix="INS", text="Electronics and instrumentation", material_type="ROH", base_uom="EA",
        price_low=Decimal("4.5"), price_high=Decimal(6400),
        weight_kg_low=Decimal("0.02"), weight_kg_high=Decimal(14),
        lead_time_days=(21, 240), quality_critical=True,
        descriptors=("Pressure transmitter", "Temperature sensor", "Flow meter", "Level switch",
                     "HMI panel", "Industrial ethernet switch", "Signal conditioner", "Power supply"),
        materials_of_construction=("SS 316 housing", "Aluminium housing", "Polycarbonate"),
        attribute_ranges={
            "accuracy_pct": (0.05, 2.5, "PCT"),
            "supply_voltage_v": (12, 240, "V"),
            "ip_rating": (54, 69, ""),
        },
    ),
    MaterialGroupSpec(
        code="MG-SERVICE", prefix="SRV", text="Services and subcontracting", material_type="DIEN", base_uom="H",
        price_low=Decimal(28), price_high=Decimal(320),
        weight_kg_low=Decimal(0), weight_kg_high=Decimal(0),
        lead_time_days=(3, 45), quality_critical=False,
        descriptors=("Heat treatment", "Surface coating", "Calibration service", "CNC subcontracting",
                     "Welding service", "Non-destructive testing", "Equipment overhaul"),
        materials_of_construction=(),
        attribute_ranges={},
    ),
)


# Supplier naming components, by region. Real vendor masters are messy and
# multilingual; so is this one.
SUPPLIER_PREFIXES: dict[str, tuple[str, ...]] = {
    "US": ("Midwest", "Great Lakes", "Summit", "Precision", "Apex", "Cardinal", "Ironclad",
           "Liberty", "Pioneer", "Meridian", "Keystone", "Bluegrass"),
    "DE": ("Nord", "Rhein", "Schwarzwald", "Bavaria", "Elbe", "Westfalen", "Alpen", "Donau"),
    "IT": ("Lombardia", "Veneto", "Adriatica", "Emiliana", "Toscana", "Piemonte"),
    "CN": ("Ningbo", "Wenzhou", "Suzhou", "Dongguan", "Qingdao", "Hangzhou", "Foshan"),
    "IN": ("Bharat", "Deccan", "Ganga", "Sahyadri", "Konkan", "Vindhya", "Ashok"),
    "JP": ("Sakura", "Nihon", "Kyowa", "Meiwa", "Tsubaki", "Kanto"),
    "TR": ("Anadolu", "Marmara", "Ege", "Toros"),
    "PL": ("Wisla", "Silesia", "Baltyk", "Karpaty"),
    "MX": ("Norte", "Bajio", "Aztec", "Sierra"),
    "KR": ("Hanil", "Daeyang", "Sungjin", "Kumho"),
    "CZ": ("Moravia", "Bohemia", "Vltava"),
    "ES": ("Iberia", "Catalana", "Levante"),
    "GB": ("Pennine", "Thames", "Clyde", "Severn"),
    "SE": ("Norrland", "Vasa", "Skane"),
    "VN": ("Mekong", "Truong Son", "Hai Phong"),
}

SUPPLIER_CORES: tuple[str, ...] = (
    "Industrial", "Engineering", "Manufacturing", "Components", "Precision", "Fluid Power",
    "Hydraulics", "Bearings", "Fasteners", "Automation", "Metals", "Machining", "Seals",
    "Electric", "Instruments", "Tooling", "Forge", "Castings", "Technologies",
)

SUPPLIER_SUFFIXES: dict[str, tuple[str, ...]] = {
    "US": ("Inc.", "LLC", "Corp.", "Co."),
    "DE": ("GmbH", "GmbH & Co. KG", "AG"),
    "IT": ("S.p.A.", "S.r.l."),
    "CN": ("Co., Ltd.", "Group Co., Ltd."),
    "IN": ("Pvt. Ltd.", "Industries Ltd."),
    "JP": ("K.K.", "Co., Ltd."),
    "TR": ("A.S.", "Ltd. Sti."),
    "PL": ("Sp. z o.o.", "S.A."),
    "MX": ("S.A. de C.V.", "S. de R.L."),
    "KR": ("Co., Ltd.", "Inc."),
    "CZ": ("s.r.o.", "a.s."),
    "ES": ("S.L.", "S.A."),
    "GB": ("Ltd", "PLC"),
    "SE": ("AB", "AB"),
    "VN": ("JSC", "Co., Ltd."),
}

# (country, weight, currency, typical incoterm, base risk, ocean/air transit days)
SUPPLIER_COUNTRIES: tuple[tuple[str, float, str, str, str, int], ...] = (
    ("US", 0.20, "USD", "FCA", "LOW", 5),
    ("DE", 0.14, "EUR", "EXW", "LOW", 12),
    ("CN", 0.16, "USD", "FOB", "MEDIUM", 34),
    ("IN", 0.09, "USD", "FOB", "MEDIUM", 30),
    ("IT", 0.07, "EUR", "EXW", "LOW", 14),
    ("MX", 0.06, "USD", "FCA", "MEDIUM", 6),
    ("PL", 0.05, "PLN", "EXW", "LOW", 15),
    ("CZ", 0.04, "CZK", "EXW", "LOW", 15),
    ("TR", 0.04, "EUR", "FOB", "MEDIUM", 20),
    ("JP", 0.04, "JPY", "FOB", "LOW", 26),
    ("KR", 0.03, "USD", "FOB", "LOW", 28),
    ("GB", 0.03, "GBP", "EXW", "LOW", 13),
    ("ES", 0.02, "EUR", "EXW", "LOW", 16),
    ("SE", 0.02, "SEK", "EXW", "LOW", 14),
    ("VN", 0.01, "USD", "FOB", "MEDIUM", 32),
)

CITIES: dict[str, tuple[str, ...]] = {
    "US": ("Cleveland", "Milwaukee", "Houston", "Charlotte", "Pittsburgh", "Grand Rapids"),
    "DE": ("Stuttgart", "Dortmund", "Nuremberg", "Bielefeld", "Mannheim", "Chemnitz"),
    "CN": ("Ningbo", "Suzhou", "Dongguan", "Qingdao", "Wenzhou", "Foshan"),
    "IN": ("Pune", "Coimbatore", "Rajkot", "Ludhiana", "Chennai", "Ahmedabad"),
    "IT": ("Brescia", "Modena", "Vicenza", "Bergamo", "Turin"),
    "MX": ("Monterrey", "Queretaro", "Saltillo", "Guadalajara"),
    "PL": ("Katowice", "Poznan", "Wroclaw", "Gdansk"),
    "CZ": ("Brno", "Ostrava", "Plzen"),
    "TR": ("Izmir", "Bursa", "Konya", "Gaziantep"),
    "JP": ("Nagoya", "Osaka", "Hamamatsu"),
    "KR": ("Busan", "Changwon", "Incheon"),
    "GB": ("Sheffield", "Birmingham", "Leeds", "Glasgow"),
    "ES": ("Bilbao", "Zaragoza", "Valencia"),
    "SE": ("Goteborg", "Malmo", "Vasteras"),
    "VN": ("Hai Phong", "Bien Hoa", "Da Nang"),
}

CAPABILITY_TAGS: dict[str, tuple[str, ...]] = {
    "MG-VALVE": ("valves", "flow control", "casting", "machining", "pressure equipment"),
    "MG-BEARING": ("bearings", "precision grinding", "heat treatment", "rotating equipment"),
    "MG-HYD": ("hydraulics", "pneumatics", "fluid power", "assembly", "testing"),
    "MG-ELEC": ("electrical", "motors", "drives", "panel building", "control systems"),
    "MG-FAST": ("fasteners", "cold forming", "thread rolling", "plating"),
    "MG-SEAL": ("seals", "elastomers", "moulding", "PTFE machining"),
    "MG-RAW": ("steel", "stockholding", "cutting", "metallurgy"),
    "MG-CHEM": ("lubricants", "chemicals", "blending", "REACH"),
    "MG-TOOL": ("cutting tools", "carbide", "regrinding", "tool management"),
    "MG-PACK": ("packaging", "corrugated", "timber", "ISPM-15"),
    "MG-ELECTRONIC": ("instrumentation", "sensors", "calibration", "electronics"),
    "MG-SERVICE": ("heat treatment", "coating", "subcontract machining", "NDT", "calibration"),
}

CERTIFICATIONS: tuple[tuple[str, float], ...] = (
    ("ISO 9001:2015", 0.82),
    ("ISO 14001:2015", 0.44),
    ("IATF 16949:2016", 0.21),
    ("ISO 45001:2018", 0.27),
    ("AS9100D", 0.07),
    ("PED 2014/68/EU", 0.13),
    ("ATEX", 0.08),
    ("ISO 17025", 0.06),
)

PAYMENT_TERMS: tuple[tuple[str, float], ...] = (
    ("NET 30", 0.34),
    ("NET 45", 0.20),
    ("NET 60", 0.19),
    ("2/10 NET 30", 0.09),
    ("NET 90", 0.07),
    ("NET 15", 0.05),
    ("50% ADVANCE, 50% NET 30", 0.03),
    ("LC AT SIGHT", 0.02),
    ("CIA", 0.01),
)

BUYERS: tuple[tuple[str, str, str, Decimal], ...] = (
    ("dana.buyer", "Dana Whitfield", "BUYER", Decimal(25_000)),
    ("sam.senior", "Sam Okonkwo", "SENIOR_BUYER", Decimal(100_000)),
    ("alex.category", "Alex Rivera", "CATEGORY_MANAGER", Decimal(250_000)),
    ("jordan.head", "Jordan Meyer", "PROCUREMENT_HEAD", Decimal(1_000_000)),
    ("priya.engineer", "Priya Nair", "ENGINEER", Decimal(0)),
    ("quinn.quality", "Quinn Alvarez", "QUALITY", Decimal(0)),
    ("morgan.finance", "Morgan Reyes", "FINANCE", Decimal(500_000)),
    ("taylor.exec", "Taylor Brooks", "EXECUTIVE", Decimal(5_000_000)),
    ("casey.auditor", "Casey Lin", "AUDITOR", Decimal(0)),
    ("admin", "System Administrator", "ADMIN", Decimal(0)),
)

DEPARTMENTS: tuple[str, ...] = (
    "Maintenance", "Production", "Engineering", "Quality", "Facilities", "Tooling", "R&D", "Logistics",
)

# Approximate mid-market rates against USD at the series start (2020), before
# the generator applies drift and volatility.
FX_START: dict[str, Decimal] = {
    "EUR": Decimal("0.89"), "GBP": Decimal("0.78"), "JPY": Decimal("107.5"),
    "CNY": Decimal("6.95"), "INR": Decimal("74.5"), "MXN": Decimal("21.4"),
    "PLN": Decimal("3.95"), "CZK": Decimal("23.4"), "TRY": Decimal("6.85"),
    "KRW": Decimal("1180"), "SEK": Decimal("9.35"), "CHF": Decimal("0.95"),
    "CAD": Decimal("1.35"), "BRL": Decimal("4.9"), "VND": Decimal("23200"),
}

# Annualised drift per currency against USD, expressed as a multiplicative
# factor per year. Emerging-market currencies depreciate; this is what makes a
# 2021 CNY price and a 2026 CNY price genuinely non-comparable without FX.
FX_ANNUAL_DRIFT: dict[str, Decimal] = {
    "EUR": Decimal("1.012"), "GBP": Decimal("1.008"), "JPY": Decimal("1.062"),
    "CNY": Decimal("1.018"), "INR": Decimal("1.035"), "MXN": Decimal("0.985"),
    "PLN": Decimal("1.005"), "CZK": Decimal("0.994"), "TRY": Decimal("1.31"),
    "KRW": Decimal("1.022"), "SEK": Decimal("1.028"), "CHF": Decimal("0.985"),
    "CAD": Decimal("1.006"), "BRL": Decimal("1.04"), "VND": Decimal("1.015"),
}

# Category-level annual inflation. Electronics deflate slightly in real terms,
# raw material tracks commodity cycles, services inflate with wages.
CATEGORY_INFLATION: dict[str, float] = {
    "MG-VALVE": 0.041, "MG-BEARING": 0.036, "MG-HYD": 0.045, "MG-ELEC": 0.033,
    "MG-FAST": 0.029, "MG-SEAL": 0.031, "MG-RAW": 0.058, "MG-CHEM": 0.052,
    "MG-TOOL": 0.027, "MG-PACK": 0.048, "MG-ELECTRONIC": 0.012, "MG-SERVICE": 0.055,
}

SCALE_PRESETS: dict[str, dict[str, int]] = {
    "tiny":   {"materials": 120,    "vendors": 25,   "po_lines": 2_000,    "years": 2},
    "small":  {"materials": 800,    "vendors": 90,   "po_lines": 25_000,   "years": 3},
    "medium": {"materials": 3_500,  "vendors": 260,  "po_lines": 180_000,  "years": 5},
    "large":  {"materials": 12_000, "vendors": 700,  "po_lines": 900_000,  "years": 6},
    "xlarge": {"materials": 30_000, "vendors": 1500, "po_lines": 3_000_000, "years": 7},
}
