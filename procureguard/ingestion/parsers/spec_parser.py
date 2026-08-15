"""Stage 5 - requirement extraction from engineering specifications.

Turns prose into atomic, machine-checkable requirements. The payoff is that
technical comparison becomes arithmetic: once "wall thickness 3.2 mm +/- 0.2" is
a TOLERANCE requirement with a numeric target, deciding whether a supplier's
3.05 mm complies is a comparison, not a judgement call.

Obligation follows the source's own binding language - shall/must/minimum are
MANDATORY, should/preferred/target are DESIRABLE - because that distinction is
what decides whether a deviation disqualifies a bid or merely costs it points.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from procureguard.domain.enums import ComparisonOperator, RequirementKind, RequirementObligation
from procureguard.domain.units import normalize_engineering_unit
from procureguard.observability import logger

log = logger(__name__)

NUM = r"[-+]?\d+(?:[.,]\d+)?"
# Trailing (?![A-Za-z]) is load-bearing: without it the bare Celsius unit "c"
# matches the "C" of "3.2 Compliance", turning a section heading into a measured
# value and silently collapsing the whole document into one section.
UNIT = (
    r"(?:mm|cm|km|µm|um|micron|in(?:ch(?:es)?)?|ft"
    r"|kg|mg|lb|oz"
    r"|bar|psi|kpa|mpa|pa|atm"
    r"|°c|°f|degc|degf|deg|°"
    r"|kv|mv|ma|kw|kva|khz|hz|hp"
    r"|ml|m3|m³|cfm|lpm|gpm"
    r"|kn|nm|ra|rz|hrc|hb|hv|db|rpm|%"
    r"|hrs?|hours?|days?|weeks?|months?|years?|min(?:utes?)?|sec(?:onds?)?"
    r"|[cfkgmlnvwats])(?![A-Za-z])"
)

# Language that makes a requirement binding versus advisory.
MANDATORY_WORDS = (
    "shall", "must", "required", "mandatory", "is to be", "has to be", "minimum",
    "maximum", "not less than", "not more than", "at least", "no greater than",
)
DESIRABLE_WORDS = ("should", "preferred", "preferably", "desirable", "target", "ideally", "nice to have")

_ATTRIBUTE_KIND: tuple[tuple[str, RequirementKind], ...] = (
    (r"pressure|bar\b|psi|mpa", RequirementKind.PERFORMANCE),
    (r"temperature|thermal|°c|°f|degc", RequirementKind.ENVIRONMENTAL),
    (r"voltage|current|power|electrical|amp|watt|hz|insulation|ip\s?\d{2}", RequirementKind.ELECTRICAL),
    (r"material|alloy|steel|stainless|brass|bronze|aluminium|aluminum|ptfe|epdm|nbr|viton|cast iron"
     r"|coating|paint|plating|galvani[sz]|primer|seal\b|gasket", RequirementKind.MATERIAL),
    (r"dimension|length|width|height|diameter|thickness|bore|pitch|clearance|tolerance|dn\b|od\b|id\b",
     RequirementKind.DIMENSIONAL),
    (r"torque|load|strength|tensile|yield|hardness|fatigue|vibration|rpm|speed|flow"
     r"|surface finish|roughness|\bra\b|\brz\b|\bhrc\b|\bhv\b", RequirementKind.MECHANICAL),
    (r"iso\s?\d+|asme|ansi|din|en\s?\d+|astm|api\s?\d+|jis|bs\s?\d+|iec|ce mark|atex|ul\b",
     RequirementKind.STANDARD_COMPLIANCE),
    (r"certificat|iso\s?9001|iso\s?14001|iatf|accredit|approval", RequirementKind.CERTIFICATION),
    (r"test report|inspection|ndt|hydrotest|mill certificate|10204|traceab|coc\b|quality plan",
     RequirementKind.QUALITY),
    (r"document|drawing|manual|datasheet|declaration|msds|sds\b", RequirementKind.DOCUMENTATION),
    (r"packag|crate|palletis|pallet|marking|labell?ing|preservation", RequirementKind.PACKAGING),
    (r"deliver|lead ?time|dispatch|ship", RequirementKind.DELIVERY),
    (r"warrant|guarantee", RequirementKind.WARRANTY),
    (r"price|payment|incoterm|currency|validity", RequirementKind.COMMERCIAL),
)


@dataclass(slots=True)
class ExtractedRequirement:
    requirement_key: str
    kind: str
    obligation: str
    attribute: str
    operator: str
    raw_text: str
    target_value: str = ""
    target_numeric: Decimal | None = None
    lower_numeric: Decimal | None = None
    upper_numeric: Decimal | None = None
    tolerance_plus: Decimal | None = None
    tolerance_minus: Decimal | None = None
    uom: str = ""
    allowed_values: list[str] = field(default_factory=list)
    weight: Decimal = Decimal(1)
    source_location: str = ""
    source_document_version_id: str = ""
    confidence: Decimal = Decimal("0.7")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_key": self.requirement_key,
            "kind": self.kind,
            "obligation": self.obligation,
            "attribute": self.attribute,
            "operator": self.operator,
            "raw_text": self.raw_text,
            "target_value": self.target_value,
            "target_numeric": self.target_numeric,
            "lower_numeric": self.lower_numeric,
            "upper_numeric": self.upper_numeric,
            "tolerance_plus": self.tolerance_plus,
            "tolerance_minus": self.tolerance_minus,
            "uom": self.uom,
            "allowed_values": self.allowed_values,
            "weight": self.weight,
            "source_location": self.source_location,
            "source_document_version_id": self.source_document_version_id,
            "extraction_confidence": self.confidence,
        }


class SpecificationParser:
    """Deterministic requirement extraction."""

    # Ordered by specificity: the first pattern that matches a statement wins,
    # so "3.2 mm +/- 0.2" becomes a TOLERANCE rather than a bare EQ on 3.2.
    def extract(
        self,
        text: str,
        *,
        source_location: str = "",
        document_version_id: str = "",
        key_prefix: str = "REQ",
        start_index: int = 1,
    ) -> list[ExtractedRequirement]:
        found: list[ExtractedRequirement] = []
        seen: set[tuple[str, str, str]] = set()
        counter = start_index

        for statement, location in self._statements(text, source_location):
            requirement = self._parse_statement(statement)
            if requirement is None:
                continue
            signature = (
                requirement.attribute.lower(),
                requirement.operator,
                (requirement.target_value or str(requirement.target_numeric or "")).lower(),
            )
            if signature in seen:
                continue
            seen.add(signature)
            requirement.requirement_key = f"{key_prefix}-{counter:03d}"
            requirement.source_location = location
            requirement.source_document_version_id = document_version_id
            found.append(requirement)
            counter += 1
        return found

    # ---------------------------------------------------------------- splitting
    @staticmethod
    def _statements(text: str, base_location: str) -> Iterator[tuple[str, str]]:
        """Yield candidate requirement statements with a source location."""
        current_section = base_location
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue

            heading = re.match(r"^(\d+(?:\.\d+){0,3})[.)]?\s+(.{3,90})$", line)
            if heading and not re.search(NUM + r"\s*" + UNIT, line, re.I):
                current_section = f"{base_location} §{heading.group(1)}".strip()
                continue
            clause = re.match(r"^(\d+(?:\.\d+){1,3})[.)]?\s+(.*)$", line)
            location = current_section
            if clause:
                location = f"{base_location} §{clause.group(1)}".strip()
                line = clause.group(2).strip()

            line = line.lstrip("-*•·\t ").strip()
            if len(line) < 6:
                continue

            # Table rows ("Attribute<tab>Value") are already atomic.
            if "\t" in line:
                yield line, location
                continue
            # Split prose into sentences and semicolon-separated clauses.
            for part in re.split(r"(?<=[.;])\s+(?=[A-Z0-9])", line):
                part = part.strip().rstrip(".")
                if len(part) >= 6:
                    yield part, location

    # ------------------------------------------------------------------ parsing
    def _parse_statement(self, statement: str) -> ExtractedRequirement | None:
        text = re.sub(r"\s+", " ", statement).strip()
        if not text or _is_boilerplate(text):
            return None

        obligation = self._obligation(text)
        for parser in (
            self._parse_tolerance,
            self._parse_lead_time,
            self._parse_range,
            self._parse_bound,
            self._parse_standard,
            self._parse_certification,
            self._parse_enumeration,
            self._parse_key_value,
            self._parse_shall_be,
            self._parse_capability,
        ):
            requirement = parser(text, obligation)
            if requirement is not None:
                requirement.kind = requirement.kind or _classify(requirement.attribute + " " + text)
                # A stated tolerance band is a control characteristic; drawings
                # do not print optional tolerances.
                if (
                    requirement.operator == ComparisonOperator.TOLERANCE.value
                    and requirement.obligation == RequirementObligation.INFORMATIONAL.value
                ):
                    requirement.obligation = RequirementObligation.MANDATORY.value
                return requirement
        return None

    @staticmethod
    def _obligation(text: str) -> str:
        lowered = text.lower()
        if any(word in lowered for word in DESIRABLE_WORDS):
            return RequirementObligation.DESIRABLE.value
        if any(word in lowered for word in MANDATORY_WORDS):
            return RequirementObligation.MANDATORY.value
        # A bare "Attribute: value" table row in a spec is binding by default;
        # engineering does not list optional dimensions in a drawing table.
        if re.match(r"^[A-Za-z][\w \-/()]{2,40}\s*[:\t]", text):
            return RequirementObligation.MANDATORY.value
        return RequirementObligation.INFORMATIONAL.value

    # ------------------------------------------------------------- sub-parsers
    _TOLERANCE = re.compile(
        rf"(?P<attr>[A-Za-z][\w \-/()]{{2,45}}?)\s*[:=]?\s*"
        rf"(?P<value>{NUM})\s*(?P<uom>{UNIT})?\s*"
        rf"(?:±|\+/-|\+-)\s*(?P<tol>{NUM})\s*(?P<tol_uom>{UNIT}|%)?",
        re.IGNORECASE,
    )
    _TOLERANCE_ASYMMETRIC = re.compile(
        rf"(?P<attr>[A-Za-z][\w \-/()]{{2,45}}?)\s*[:=]?\s*"
        rf"(?P<value>{NUM})\s*(?P<uom>{UNIT})?\s*"
        rf"\+\s*(?P<plus>{NUM})\s*/\s*-\s*(?P<minus>{NUM})",
        re.IGNORECASE,
    )

    def _parse_tolerance(self, text: str, obligation: str) -> ExtractedRequirement | None:
        match = self._TOLERANCE_ASYMMETRIC.search(text)
        if match:
            target = _dec(match.group("value"))
            plus, minus = _dec(match.group("plus")), _dec(match.group("minus"))
            if target is None:
                return None
            return ExtractedRequirement(
                requirement_key="",
                kind="",
                obligation=obligation,
                attribute=_clean_attribute(match.group("attr")),
                operator=ComparisonOperator.TOLERANCE.value,
                raw_text=text,
                target_numeric=target,
                tolerance_plus=plus,
                tolerance_minus=minus,
                uom=normalize_engineering_unit(match.group("uom") or ""),
                confidence=Decimal("0.9"),
            )

        match = self._TOLERANCE.search(text)
        if not match:
            return None
        target = _dec(match.group("value"))
        tolerance = _dec(match.group("tol"))
        if target is None or tolerance is None:
            return None
        # "±2%" is a proportion of the nominal, not an absolute band.
        if (match.group("tol_uom") or "").strip() == "%":
            tolerance = (target * tolerance / Decimal(100)).copy_abs()
        return ExtractedRequirement(
            requirement_key="",
            kind="",
            obligation=obligation,
            attribute=_clean_attribute(match.group("attr")),
            operator=ComparisonOperator.TOLERANCE.value,
            raw_text=text,
            target_numeric=target,
            tolerance_plus=tolerance,
            tolerance_minus=tolerance,
            uom=normalize_engineering_unit(match.group("uom") or ""),
            confidence=Decimal("0.92"),
        )

    _RANGE = re.compile(
        rf"(?P<attr>[A-Za-z][\w \-/()]{{2,45}}?)\s*[:=]?\s*"
        rf"(?:range\s*)?(?P<low>{NUM})\s*(?P<low_uom>{UNIT})?\s*"
        rf"(?:to|-|–|\.\.\.|\.\.|through|up to)\s*"
        rf"(?P<high>{NUM})\s*(?P<uom>{UNIT})?",
        re.IGNORECASE,
    )

    def _parse_range(self, text: str, obligation: str) -> ExtractedRequirement | None:
        match = self._RANGE.search(text)
        if not match:
            return None
        low, high = _dec(match.group("low")), _dec(match.group("high"))
        if low is None or high is None or low >= high:
            return None
        uom = normalize_engineering_unit(match.group("uom") or match.group("low_uom") or "")
        # Guard against matching a date, a standard number, or a part number.
        if not uom and not re.search(r"range|between|from", text, re.I):
            return None
        return ExtractedRequirement(
            requirement_key="",
            kind="",
            obligation=obligation,
            attribute=_clean_attribute(match.group("attr")),
            operator=ComparisonOperator.RANGE.value,
            raw_text=text,
            lower_numeric=low,
            upper_numeric=high,
            uom=uom,
            confidence=Decimal("0.85"),
        )

    _MIN_WORDS = r"(?:minimum|min\.?|at least|not less than|no less than|greater than or equal to|>=|≥|>)"
    _MAX_WORDS = r"(?:maximum|max\.?|at most|not more than|no more than|not exceed(?:ing)?|less than or equal to|<=|≤|<)"
    _BOUND_PREFIX = re.compile(
        rf"(?P<attr>[A-Za-z][\w \-/()]{{2,45}}?)\s*[:=]?\s*"
        rf"(?:shall|must|should|to)?\s*(?:be\s*)?(?P<dir>{_MIN_WORDS}|{_MAX_WORDS})\s*"
        rf"(?P<value>{NUM})\s*(?P<uom>{UNIT})?",
        re.IGNORECASE,
    )
    _BOUND_SUFFIX = re.compile(
        rf"(?P<dir>{_MIN_WORDS}|{_MAX_WORDS})\s*(?P<value>{NUM})\s*(?P<uom>{UNIT})?\s*"
        rf"(?:of\s+)?(?P<attr>[A-Za-z][\w \-/()]{{2,45}})",
        re.IGNORECASE,
    )
    # "Maximum weight 45 kg" - direction, then attribute, then value. Common in
    # drawing notes and parameter tables, and matched by neither of the above.
    _BOUND_INFIX = re.compile(
        rf"^(?P<dir>{_MIN_WORDS}|{_MAX_WORDS})\s+(?P<attr>[A-Za-z][\w \-/()]{{2,45}}?)\s+"
        rf"(?:of\s+|is\s+|shall\s+be\s+)?(?P<value>{NUM})\s*(?P<uom>{UNIT})?",
        re.IGNORECASE,
    )

    def _parse_bound(self, text: str, obligation: str) -> ExtractedRequirement | None:
        for pattern in (self._BOUND_PREFIX, self._BOUND_INFIX, self._BOUND_SUFFIX):
            match = pattern.search(text)
            if not match:
                continue
            value = _dec(match.group("value"))
            if value is None:
                continue
            direction = match.group("dir").lower()
            is_minimum = bool(re.match(self._MIN_WORDS, direction, re.I))
            return ExtractedRequirement(
                requirement_key="",
                kind="",
                obligation=obligation,
                attribute=_clean_attribute(match.group("attr")),
                operator=(
                    ComparisonOperator.GTE.value if is_minimum else ComparisonOperator.LTE.value
                ),
                raw_text=text,
                target_numeric=value,
                uom=normalize_engineering_unit(match.group("uom") or ""),
                confidence=Decimal("0.88"),
            )
        return None

    # Standards frequently carry a letter section between body and number
    # ("ASME B16.34", "ASTM A216", "MIL-STD-810"), so the letter block is
    # optional rather than absent.
    _STANDARD = re.compile(
        r"\b(?P<std>(?:ISO|EN|DIN|ASME|ASTM|ANSI|API|BS|JIS|IEC|UL|NFPA|AWS|SAE|NACE|MIL(?:-STD)?)"
        r"[\s-]?(?:[A-Z]{1,2}[\s-]?)?\d{2,6}(?:[-.:]\d+)*(?:[-/][A-Z0-9]+)?)",
        re.IGNORECASE,
    )

    def _parse_standard(self, text: str, obligation: str) -> ExtractedRequirement | None:
        matches = self._STANDARD.findall(text)
        if not matches:
            return None
        if not re.search(
            r"compl(?:y|iance|iant)|accordance|per\b|to\b|conform|as per|standard|design(?:ed)? to|manufactur",
            text,
            re.I,
        ):
            return None
        standard = re.sub(r"\s+", " ", matches[0]).upper().strip()
        return ExtractedRequirement(
            requirement_key="",
            kind=RequirementKind.STANDARD_COMPLIANCE.value,
            obligation=(
                RequirementObligation.MANDATORY.value
                if obligation == RequirementObligation.INFORMATIONAL.value
                else obligation
            ),
            attribute=f"Compliance with {standard}",
            operator=ComparisonOperator.BOOLEAN.value,
            raw_text=text,
            target_value="yes",
            weight=Decimal("2"),
            confidence=Decimal("0.9"),
        )

    _CERTIFICATION = re.compile(
        r"(?P<cert>ISO\s?9001|ISO\s?14001|IATF\s?16949|AS\s?9100|ISO\s?45001|CE\s?mark(?:ing)?|"
        r"ATEX|UKCA|RoHS|REACH|EN\s?10204\s?3\.[12]|material test certificate|mill certificate|"
        r"certificate of conformity|CoC|PED|NACE)",
        re.IGNORECASE,
    )

    def _parse_certification(self, text: str, obligation: str) -> ExtractedRequirement | None:
        match = self._CERTIFICATION.search(text)
        if not match:
            return None
        certificate = re.sub(r"\s+", " ", match.group("cert")).strip()
        is_document = bool(
            re.search(r"certificate|report|10204|conformity|mill", certificate, re.I)
        )
        return ExtractedRequirement(
            requirement_key="",
            kind=(
                RequirementKind.DOCUMENTATION.value
                if is_document
                else RequirementKind.CERTIFICATION.value
            ),
            obligation=(
                RequirementObligation.MANDATORY.value
                if obligation == RequirementObligation.INFORMATIONAL.value
                else obligation
            ),
            attribute=f"{certificate} required",
            operator=ComparisonOperator.BOOLEAN.value,
            raw_text=text,
            target_value="yes",
            weight=Decimal("2"),
            confidence=Decimal("0.88"),
        )

    _ENUM = re.compile(
        r"(?P<attr>[A-Za-z][\w \-/()]{2,45}?)\s*[:=]\s*(?P<values>[\w\-.+ ]+(?:\s*(?:/|,|or)\s*[\w\-.+ ]+){1,5})$",
        re.IGNORECASE,
    )

    def _parse_enumeration(self, text: str, obligation: str) -> ExtractedRequirement | None:
        match = self._ENUM.search(text)
        if not match:
            return None
        raw_values = re.split(r"\s*(?:/|,|\bor\b)\s*", match.group("values"))
        values = [v.strip() for v in raw_values if v.strip() and len(v.strip()) <= 40]
        if len(values) < 2 or any(re.fullmatch(NUM, v) for v in values):
            return None
        return ExtractedRequirement(
            requirement_key="",
            kind="",
            obligation=obligation,
            attribute=_clean_attribute(match.group("attr")),
            operator=ComparisonOperator.ONE_OF.value,
            raw_text=text,
            allowed_values=values,
            confidence=Decimal("0.75"),
        )

    _KEY_VALUE = re.compile(
        r"^(?P<attr>[A-Za-z][\w \-/()]{2,45}?)\s*[:\t=]\s*(?P<value>.{1,80}?)\s*$",
        re.IGNORECASE,
    )

    def _parse_key_value(self, text: str, obligation: str) -> ExtractedRequirement | None:
        match = self._KEY_VALUE.match(text)
        if not match:
            return None
        value = match.group("value").strip()
        if not value or value.lower() in ("n/a", "na", "tbd", "-", "none"):
            return None

        numeric_match = re.fullmatch(rf"(?P<num>{NUM})\s*(?P<uom>{UNIT})?", value, re.IGNORECASE)
        if numeric_match:
            return ExtractedRequirement(
                requirement_key="",
                kind="",
                obligation=obligation,
                attribute=_clean_attribute(match.group("attr")),
                operator=ComparisonOperator.EQ.value,
                raw_text=text,
                target_value=value,
                target_numeric=_dec(numeric_match.group("num")),
                uom=normalize_engineering_unit(numeric_match.group("uom") or ""),
                confidence=Decimal("0.82"),
            )
        return ExtractedRequirement(
            requirement_key="",
            kind="",
            obligation=obligation,
            attribute=_clean_attribute(match.group("attr")),
            operator=ComparisonOperator.EQ.value,
            raw_text=text,
            target_value=value,
            confidence=Decimal("0.7"),
        )

    _LEAD_TIME = re.compile(
        r"(?P<attr>delivery|lead[\s-]?time|dispatch|shipment|warranty|guarantee)\b[^.\n]{0,25}?"
        r"(?P<dir>within|in|of|not exceeding|no later than|maximum|max\.?|minimum|min\.?|:)?\s*"
        rf"(?P<value>{NUM})\s*(?P<uom>days?|weeks?|months?|years?|hrs?|hours?)\b",
        re.IGNORECASE,
    )

    def _parse_lead_time(self, text: str, obligation: str) -> ExtractedRequirement | None:
        """Delivery and warranty durations, whose direction is not symmetric.

        Shorter delivery is always better and longer warranty is always better,
        so the two map to opposite operators from the same sentence shape.
        """
        match = self._LEAD_TIME.search(text)
        if not match:
            return None
        value = _dec(match.group("value"))
        if value is None:
            return None
        attribute = _clean_attribute(match.group("attr"))
        is_warranty = bool(re.match(r"warrant|guarantee", attribute, re.I))
        return ExtractedRequirement(
            requirement_key="",
            kind=(
                RequirementKind.WARRANTY.value
                if is_warranty
                else RequirementKind.DELIVERY.value
            ),
            obligation=(
                RequirementObligation.MANDATORY.value
                if obligation == RequirementObligation.INFORMATIONAL.value
                else obligation
            ),
            attribute=attribute,
            operator=(
                ComparisonOperator.GTE.value if is_warranty else ComparisonOperator.LTE.value
            ),
            raw_text=text,
            target_numeric=value,
            uom=normalize_engineering_unit(match.group("uom")),
            confidence=Decimal("0.85"),
        )

    _SHALL_BE = re.compile(
        r"^(?P<attr>[A-Za-z][\w \-/()]{2,45}?)\s+"
        r"(?:shall|must|should|is to|are to|will)\s+be\s+"
        r"(?P<value>[\w\-.+/ ]{2,60}?)\s*$",
        re.IGNORECASE,
    )

    def _parse_shall_be(self, text: str, obligation: str) -> ExtractedRequirement | None:
        match = self._SHALL_BE.match(text)
        if not match:
            return None
        value = match.group("value").strip()
        if not value or value.lower() in ("provided", "supplied", "included", "required", "used"):
            return None
        return ExtractedRequirement(
            requirement_key="",
            kind="",
            obligation=obligation,
            attribute=_clean_attribute(match.group("attr")),
            operator=ComparisonOperator.EQ.value,
            raw_text=text,
            target_value=value,
            confidence=Decimal("0.78"),
        )

    _CAPABILITY = re.compile(
        r"(?P<subject>[A-Za-z][\w \-/()]{2,55}?)\s+"
        r"(?:shall|must|is|are|to)\s+(?:be\s+)?(?P<state>required|provided|supplied|included|"
        r"available|guaranteed|certified|tested|inspected|painted|coated|marked|traceable)",
        re.IGNORECASE,
    )

    def _parse_capability(self, text: str, obligation: str) -> ExtractedRequirement | None:
        match = self._CAPABILITY.search(text)
        if not match:
            return None
        return ExtractedRequirement(
            requirement_key="",
            kind="",
            obligation=(
                RequirementObligation.MANDATORY.value
                if obligation == RequirementObligation.INFORMATIONAL.value
                else obligation
            ),
            attribute=_clean_attribute(match.group("subject")),
            operator=ComparisonOperator.BOOLEAN.value,
            raw_text=text,
            target_value="yes",
            confidence=Decimal("0.7"),
        )


def _classify(text: str) -> str:
    lowered = text.lower()
    for pattern, kind in _ATTRIBUTE_KIND:
        if re.search(pattern, lowered):
            return kind.value
    return RequirementKind.OTHER.value


def _clean_attribute(raw: str) -> str:
    text = re.sub(r"\s+", " ", (raw or "").strip(" -:\t"))
    text = re.sub(
        r"^(?:the|a|an|all|each|any|its|shall|must|should|be|is|are|to|of|with|for)\s+",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+(?:shall|must|should|is|are|be|to)$", "", text, flags=re.I)
    return text.strip()[:255] or "Unspecified attribute"


def _dec(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        return Decimal(value.replace(",", "."))
    except (InvalidOperation, AttributeError):
        return None


_BOILERPLATE = re.compile(
    r"^(?:page \d+|rev(?:ision)? \w+|sheet \d+|drawn by|checked by|approved by|date|scale|"
    r"confidential|proprietary|copyright|all rights reserved|table of contents|"
    r"this document|note[s]?:?|general|introduction|scope|purpose)\b",
    re.IGNORECASE,
)


def _is_boilerplate(text: str) -> bool:
    if _BOILERPLATE.match(text):
        return True
    # Mostly punctuation or a separator rule.
    alnum = sum(1 for c in text if c.isalnum())
    return alnum < max(4, len(text) // 4)
