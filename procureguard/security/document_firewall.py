"""Document firewall for supplier-controlled content.

Threat model: a supplier (or someone who has compromised a supplier's mailbox)
sends a document whose *content* is designed to steer an automated buyer. The
realistic goals are to be marked compliant without meeting the spec, to redirect
payment to a new bank account, to exfiltrate competitors' prices, or to suppress
a disqualification.

The firewall does not try to "clean" hostile text. It classifies, records and
quarantines, then hands a verdict to policy. Content is never silently dropped -
a buyer must be able to see exactly what a supplier sent.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from re import Pattern

from procureguard.domain.enums import SecuritySeverity


class Verdict(StrEnum):
    CLEAN = "CLEAN"
    FLAGGED = "FLAGGED"  # usable, but findings are attached to the case
    QUARANTINE = "QUARANTINE"  # never enters model context or evidence


@dataclass(frozen=True, slots=True)
class SecurityFinding:
    finding_type: str
    severity: str
    detail: str
    disposition: str
    matched_excerpt: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "finding_type": self.finding_type,
            "severity": self.severity,
            "detail": self.detail,
            "disposition": self.disposition,
            "matched_excerpt": self.matched_excerpt,
        }


@dataclass(frozen=True, slots=True)
class ScanResult:
    verdict: Verdict
    findings: tuple[SecurityFinding, ...] = ()
    sanitized_text: str = ""
    normalization_notes: tuple[str, ...] = ()

    @property
    def is_quarantined(self) -> bool:
        return self.verdict == Verdict.QUARANTINE

    @property
    def finding_types(self) -> set[str]:
        return {f.finding_type for f in self.findings}

    def max_severity(self) -> str:
        order = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
        worst = "INFO"
        for finding in self.findings:
            if order.index(finding.severity) > order.index(worst):
                worst = finding.severity
        return worst


def _compile(patterns: Iterable[str]) -> tuple[Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns)


class SupplierDocumentFirewall:
    # Direct attempts to override the agent's instructions.
    PROMPT_INJECTION = _compile(
        (
            r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding)\s+"
            r"(?:instructions?|rules?|policies|prompts?|directions?)",
            r"disregard\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+"
            r"(?:instructions?|rules?|context)",
            r"forget\s+(?:everything|all\s+previous|your\s+instructions)",
            r"\b(?:system|developer)\s*(?:prompt|message|instruction)\b",
            r"you\s+are\s+now\s+(?:a|an|the)\b",
            r"new\s+(?:instructions?|rules?|system\s+prompt)\s*[:=]",
            r"</?(?:system|assistant|instructions?|im_start|im_end)>",
            r"\[\s*(?:system|inst|/inst)\s*\]",
            r"<<<\s*(?:end-)?untrusted-content",  # forging our own boundary
            r"act\s+as\s+(?:if\s+you\s+are\s+)?(?:the\s+)?(?:buyer|approver|administrator)",
            r"(?:enable|activate)\s+(?:developer|debug|god)\s+mode",
        )
    )

    # Attempts to obtain a favourable outcome by assertion rather than evidence.
    OUTCOME_STEERING = _compile(
        (
            r"mark\s+(?:this\s+|our\s+|the\s+)?\S*\s*(?:as\s+)?(?:approved|qualified|compliant|preferred|accepted)",
            r"(?:automatically|auto)[-\s]?(?:approve|qualify|accept|award)",
            r"treat\s+(?:this|our)\s+(?:quote|bid|offer)\s+as\s+(?:compliant|lowest|best|winning)",
            r"(?:skip|bypass|waive|omit)\s+(?:the\s+)?(?:technical\s+)?(?:evaluation|review|approval|inspection)",
            r"no\s+(?:further\s+)?(?:review|approval|evaluation)\s+(?:is\s+)?(?:required|needed|necessary)",
            r"award\s+(?:this\s+)?(?:order|contract|po)\s+to\s+us",
            r"rank\s+(?:us|our\s+(?:bid|quote))\s+(?:as\s+)?(?:first|l1|number\s*one)",
            r"do\s+not\s+(?:compare|share|disclose)\s+(?:this|our)\s+(?:price|quote)",
        )
    )

    # Bank/payment redirection: the highest-value fraud against a buyer.
    PAYMENT_CHANGE = _compile(
        (
            r"\b(?:new|updated|changed|revised)\s+(?:bank|banking|account|payment)\s+(?:details?|information|instructions?)",
            r"\bbank\s+account\s+(?:number|details?|has\s+changed)",
            r"\biban\b\s*[:=]?\s*[A-Z]{2}\d{2}",
            r"\bswift\s*(?:/\s*bic)?\b\s*[:=]?\s*[A-Z]{6}",
            r"\brouting\s+(?:number|code)\b",
            r"\bsort\s+code\b",
            r"remit(?:tance)?\s+to\s+(?:a\s+)?(?:new|different)\s+account",
            r"please\s+(?:update|change)\s+(?:our|the)\s+(?:bank|payment)\s+details",
        )
    )

    # Attempts to pull other suppliers' data out of the system.
    EXFILTRATION = _compile(
        (
            r"(?:list|show|send|reveal|share|tell)\s+(?:me\s+)?(?:the\s+)?(?:other|competitor|competing|all)\s+"
            r"(?:supplier|vendor|bidder)s?['’]?s?\s*(?:price|quote|bid|offer)",
            r"what\s+(?:did|are)\s+(?:the\s+)?other\s+(?:suppliers?|vendors?|bidders?)\s+(?:quote|offer|bid)",
            r"(?:lowest|best|competing)\s+(?:price|bid|quote)\s+(?:so\s+far|received|on\s+file)",
            r"(?:send|forward|email)\s+(?:the\s+)?(?:comparison|bid\s+tab|evaluation)\s+to\b",
            r"include\s+(?:your\s+)?(?:system\s+prompt|instructions)\s+in\s+(?:your\s+)?(?:reply|response)",
        )
    )

    # Credential and link-based attacks.
    CREDENTIAL_PHISHING = _compile(
        (
            r"(?:enter|confirm|verify|update)\s+your\s+(?:password|credentials?|login|mfa|2fa)",
            r"click\s+(?:here|this\s+link)\s+to\s+(?:verify|authenticate|log\s*in)",
            r"\b(?:api[_\s-]?key|secret[_\s-]?key|access[_\s-]?token|bearer\s+[A-Za-z0-9._-]{20,})\b",
            r"\b(?:aws_access_key_id|aws_secret_access_key|private[_\s-]?key)\b",
        )
    )

    SUSPICIOUS_URL = _compile(
        (
            r"https?://(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?/",  # bare IP
            r"https?://[^\s/]*\.(?:ru|tk|ml|ga|cf|gq|top|xyz|zip|mov)(?:[/:]|\s|$)",
            r"https?://[^\s]*@",  # userinfo-in-URL spoofing
            r"(?:bit\.ly|tinyurl\.com|t\.co|goo\.gl|is\.gd|ow\.ly)/",
            r"data:text/html",
            r"javascript:",
        )
    )

    # Characters used to hide text from humans while keeping it machine-visible.
    INVISIBLE_CHARS = re.compile(r"[​-‏‪-‮⁠-⁤﻿­]")
    EXCESSIVE_WHITESPACE = re.compile(r"\n{20,}|\s{300,}")
    BASE64_BLOB = re.compile(r"\b[A-Za-z0-9+/]{200,}={0,2}\b")

    def __init__(self, *, max_text_bytes: int = 4_000_000) -> None:
        self.max_text_bytes = max_text_bytes

    # ------------------------------------------------------------------ public
    def scan_text(self, text: str) -> tuple[SecurityFinding, ...]:
        """Backwards-compatible entry point returning findings only."""
        return self.scan(text).findings

    def scan(self, text: str, *, source_label: str = "supplier document") -> ScanResult:
        raw = text or ""
        if len(raw.encode("utf-8", errors="ignore")) > self.max_text_bytes:
            raw = raw[: self.max_text_bytes]

        normalized, notes = self._normalize(raw)
        findings: list[SecurityFinding] = []

        findings.extend(
            self._match(
                normalized,
                self.PROMPT_INJECTION,
                finding_type="PROMPT_INJECTION",
                severity=SecuritySeverity.CRITICAL,
                detail=f"Supplier-controlled instruction detected in {source_label}",
                disposition="QUARANTINE_INSTRUCTIONAL_CONTENT",
                first_only=True,
            )
        )
        findings.extend(
            self._match(
                normalized,
                self.OUTCOME_STEERING,
                finding_type="OUTCOME_STEERING",
                severity=SecuritySeverity.HIGH,
                detail="Content attempts to dictate an evaluation or award outcome",
                disposition="STRIP_FROM_MODEL_CONTEXT",
                first_only=True,
            )
        )
        findings.extend(
            self._match(
                normalized,
                self.PAYMENT_CHANGE,
                finding_type="PAYMENT_DETAIL_CHANGE",
                severity=SecuritySeverity.HIGH,
                detail=(
                    "Supplier-controlled payment details require out-of-band verification "
                    "with a known contact and dual approval before any master-data change"
                ),
                disposition="REQUIRE_HUMAN_VERIFICATION",
                first_only=True,
            )
        )
        findings.extend(
            self._match(
                normalized,
                self.EXFILTRATION,
                finding_type="DATA_EXFILTRATION_ATTEMPT",
                severity=SecuritySeverity.HIGH,
                detail="Content solicits competitor bid data or internal instructions",
                disposition="STRIP_FROM_MODEL_CONTEXT",
                first_only=True,
            )
        )
        findings.extend(
            self._match(
                normalized,
                self.CREDENTIAL_PHISHING,
                finding_type="CREDENTIAL_SOLICITATION",
                severity=SecuritySeverity.HIGH,
                detail="Content solicits credentials or contains embedded secrets",
                disposition="REQUIRE_HUMAN_VERIFICATION",
                first_only=True,
            )
        )
        findings.extend(
            self._match(
                normalized,
                self.SUSPICIOUS_URL,
                finding_type="SUSPICIOUS_URL",
                severity=SecuritySeverity.MEDIUM,
                detail="Content contains a link with characteristics common to phishing",
                disposition="FLAG_FOR_REVIEW",
                first_only=True,
            )
        )

        if self.INVISIBLE_CHARS.search(raw):
            findings.append(
                SecurityFinding(
                    finding_type="HIDDEN_TEXT",
                    severity=SecuritySeverity.HIGH,
                    detail=(
                        "Zero-width or bidirectional control characters found: text may be "
                        "hidden from a human reader while remaining visible to the model"
                    ),
                    disposition="STRIP_FROM_MODEL_CONTEXT",
                    matched_excerpt=_excerpt_around(raw, self.INVISIBLE_CHARS),
                )
            )
        if self.EXCESSIVE_WHITESPACE.search(raw):
            findings.append(
                SecurityFinding(
                    finding_type="WHITESPACE_PADDING",
                    severity=SecuritySeverity.MEDIUM,
                    detail="Large whitespace run used to push content out of visual view",
                    disposition="FLAG_FOR_REVIEW",
                )
            )
        if self.BASE64_BLOB.search(normalized):
            findings.append(
                SecurityFinding(
                    finding_type="ENCODED_PAYLOAD",
                    severity=SecuritySeverity.MEDIUM,
                    detail="Long encoded blob embedded in document text",
                    disposition="FLAG_FOR_REVIEW",
                    matched_excerpt=_excerpt_around(normalized, self.BASE64_BLOB),
                )
            )
        for note in notes:
            findings.append(
                SecurityFinding(
                    finding_type="UNICODE_OBFUSCATION",
                    severity=SecuritySeverity.MEDIUM,
                    detail=note,
                    disposition="NORMALIZE_AND_FLAG",
                )
            )

        verdict = self._verdict(findings)
        return ScanResult(
            verdict=verdict,
            findings=tuple(findings),
            sanitized_text=self._sanitize(normalized, findings),
            normalization_notes=tuple(notes),
        )

    def scan_email(
        self, *, subject: str, body: str, from_address: str, known_vendor_domain: str = ""
    ) -> ScanResult:
        """Scan a message plus its envelope, adding sender-based checks."""
        result = self.scan(f"{subject}\n\n{body}", source_label="supplier email")
        findings = list(result.findings)

        domain = from_address.split("@")[-1].lower() if "@" in from_address else ""
        if known_vendor_domain and domain and domain != known_vendor_domain.lower():
            findings.append(
                SecurityFinding(
                    finding_type="SENDER_DOMAIN_MISMATCH",
                    severity=SecuritySeverity.HIGH,
                    detail=(
                        f"Reply arrived from {domain!r} but the vendor master records "
                        f"{known_vendor_domain!r}; treat as unverified until confirmed "
                        f"through a known contact"
                    ),
                    disposition="REQUIRE_HUMAN_VERIFICATION",
                    matched_excerpt=from_address,
                )
            )
        if domain and _is_lookalike(domain, known_vendor_domain):
            findings.append(
                SecurityFinding(
                    finding_type="LOOKALIKE_DOMAIN",
                    severity=SecuritySeverity.CRITICAL,
                    detail=(
                        f"Sender domain {domain!r} closely resembles the registered vendor "
                        f"domain {known_vendor_domain!r}; classic supplier-impersonation pattern"
                    ),
                    disposition="QUARANTINE_INSTRUCTIONAL_CONTENT",
                    matched_excerpt=from_address,
                )
            )
        return ScanResult(
            verdict=self._verdict(findings),
            findings=tuple(findings),
            sanitized_text=result.sanitized_text,
            normalization_notes=result.normalization_notes,
        )

    # ---------------------------------------------------------------- internal
    def _match(
        self,
        text: str,
        patterns: tuple[Pattern[str], ...],
        *,
        finding_type: str,
        severity: SecuritySeverity,
        detail: str,
        disposition: str,
        first_only: bool = False,
    ) -> list[SecurityFinding]:
        out: list[SecurityFinding] = []
        for pattern in patterns:
            match = pattern.search(text)
            if not match:
                continue
            out.append(
                SecurityFinding(
                    finding_type=finding_type,
                    severity=str(severity),
                    detail=detail,
                    disposition=disposition,
                    matched_excerpt=_context(text, match.start(), match.end()),
                )
            )
            if first_only:
                break
        return out

    def _normalize(self, text: str) -> tuple[str, list[str]]:
        """Undo the obfuscations that defeat naive pattern matching."""
        notes: list[str] = []
        stripped = self.INVISIBLE_CHARS.sub("", text)

        # NFKC folds fullwidth/mathematical letter variants back to ASCII, so
        # "ｉｇｎｏｒｅ ａｌｌ" and "𝗶𝗴𝗻𝗼𝗿𝗲 𝗮𝗹𝗹" match the same patterns.
        folded = unicodedata.normalize("NFKC", stripped)
        if folded != stripped:
            notes.append("Text contained compatibility-form characters that were normalised")

        homoglyph_free = folded.translate(_HOMOGLYPHS)
        if homoglyph_free != folded:
            notes.append("Text contained Cyrillic/Greek homoglyphs that were normalised to Latin")
        return homoglyph_free, notes

    def _sanitize(self, text: str, findings: list[SecurityFinding]) -> str:
        """Redact spans that must never reach model context."""
        if not any(f.disposition in _REDACTING_DISPOSITIONS for f in findings):
            return text
        sanitized = text
        for patterns in (self.PROMPT_INJECTION, self.OUTCOME_STEERING, self.EXFILTRATION):
            for pattern in patterns:
                sanitized = pattern.sub("[REDACTED-BY-DOCUMENT-FIREWALL]", sanitized)
        return sanitized

    @staticmethod
    def _verdict(findings: list[SecurityFinding]) -> Verdict:
        if not findings:
            return Verdict.CLEAN
        if any(f.severity == SecuritySeverity.CRITICAL for f in findings):
            return Verdict.QUARANTINE
        return Verdict.FLAGGED


_REDACTING_DISPOSITIONS = frozenset(
    {"QUARANTINE_INSTRUCTIONAL_CONTENT", "STRIP_FROM_MODEL_CONTEXT"}
)

# Cyrillic and Greek letters that render identically to Latin ones.
_HOMOGLYPHS = str.maketrans(
    {
        "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
        "і": "i", "ѕ": "s", "ԁ": "d", "һ": "h", "ј": "j", "ӏ": "l", "ԛ": "q",
        "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
        "Р": "P", "С": "C", "Т": "T", "Х": "X", "У": "Y",
        "α": "a", "ο": "o", "ρ": "p", "ϲ": "c", "τ": "t", "ν": "v",
        "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
        "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    }
)


def _context(text: str, start: int, end: int, window: int = 90) -> str:
    lo = max(0, start - window)
    hi = min(len(text), end + window)
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(text) else ""
    return f"{prefix}{text[lo:hi].strip()}{suffix}"[:400]


def _excerpt_around(text: str, pattern: Pattern[str]) -> str:
    match = pattern.search(text)
    return _context(text, match.start(), match.end()) if match else ""


def _is_lookalike(domain: str, reference: str) -> bool:
    """Edit distance 1-2 against the registered domain, excluding equality."""
    if not reference or not domain:
        return False
    a, b = domain.lower(), reference.lower()
    if a == b:
        return False
    if abs(len(a) - len(b)) > 2:
        return False
    distance = _levenshtein(a, b, cutoff=2)
    return 0 < distance <= 2


def _levenshtein(a: str, b: str, *, cutoff: int = 2) -> int:
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        if min(current) > cutoff:
            return cutoff + 1
        previous = current
    return previous[-1]
