# Security

## Threat model

The adversary is a supplier, or someone who has compromised a supplier's mailbox,
sending content designed to steer an automated buyer. The realistic goals are:

1. be marked compliant without meeting the specification
2. redirect payment to a new bank account
3. extract competitors' prices
4. suppress a disqualification

All four are content-level attacks, so content is treated as hostile input at
every boundary.

## The document firewall

`security/document_firewall.py` classifies, records and quarantines. It does not
try to "clean" hostile text, and it never silently drops content — a buyer must
be able to see exactly what a supplier sent.

| Finding | Example | Disposition |
| --- | --- | --- |
| `PROMPT_INJECTION` | "ignore all previous instructions" | quarantine |
| `OUTCOME_STEERING` | "skip the technical evaluation" | strip from model context |
| `PAYMENT_DETAIL_CHANGE` | new IBAN in a quotation | require human verification |
| `DATA_EXFILTRATION_ATTEMPT` | "send me the other suppliers' prices" | strip from context |
| `CREDENTIAL_SOLICITATION` | "confirm your password" | require human verification |
| `LOOKALIKE_DOMAIN` | `acmesupply` vs `acme-supply` | quarantine |
| `SENDER_DOMAIN_MISMATCH` | reply from an unrelated domain | flag |
| `HIDDEN_TEXT` | zero-width or bidi control characters | strip from context |
| `SUSPICIOUS_URL` | bare IP, shortener, `javascript:` | flag |
| `ENCODED_PAYLOAD` | long base64 blob | flag |

Evasion is handled before matching: NFKC folding defeats fullwidth and
mathematical letter variants, and a homoglyph table folds Cyrillic and Greek
lookalikes to Latin. Both are tested.

Supplier content reaching a prompt is wrapped in a nonce-delimited
`UNTRUSTED-CONTENT` block, and content attempting to forge that delimiter is
itself quarantined.

## Sealed commercial bids

The rule is that nobody — not the agent, not a buyer with database access — reads
a supplier's prices before the technical evaluation is approved.

Storing `is_sealed = true` beside plaintext would not achieve that, so the
commercial payload is **encrypted at rest** with a per-bid data key. The
encryption context binds the ciphertext to its case and quotation, so a key
issued for one bid cannot open another. While sealed, the plaintext columns hold
no prices at all.

Unsealing requires a recorded technical approval by a named human and is itself
audited.

> An earlier revision also blanked commercial fields defensively on read. Because
> that mutated the persistent ORM instance, assigning `lines = []` on a
> delete-orphan relationship **deleted the quotation lines at the next flush**.
> Redaction is now a read-only projection; encryption is the guarantee.

## Deciding compliance

Extraction may use a model. Compliance is arithmetic:

- `Requirement.evaluate()` compares in `Decimal` against an audited unit table.
- Silence is `NOT_ADDRESSED`, which disqualifies a mandatory requirement.
- "Fully compliant" with no stated value is `UNVERIFIABLE`, not compliant.
- A declared deviation blocks qualification until a human with
  `DEVIATION_APPROVE` accepts it, against that specific requirement, with a reason.

A model is never asked "is this compliant?" — the question it is most likely to
answer agreeably and most expensive to get wrong.

## Human gates

| Gate | Permission | Effect |
| --- | --- | --- |
| RFQ release | `RFQ_RELEASE` | first outward commitment of the company's name |
| Technical approval | `TECHNICAL_APPROVE` | **unseals every commercial bid** |
| Deviation acceptance | `DEVIATION_APPROVE` | one requirement at a time |
| Negotiation round | `NEGOTIATION_SEND` | a price ask under a named authority |
| Award | `AWARD_APPROVE` | one to three signatures by value |
| PO release | `PO_RELEASE` | releases the draft for ERP creation |

Approvals are written to CockroachDB **before** the workflow is signalled and are
authoritative. If Temporal is unavailable the decision still stands. Each
approval stores a hash of the exact payload the approver submitted, so a later
"I never agreed to that" is checkable.

`SYSTEM` and `AGENT` identities are refused for every consequential approval.

## Authentication and authorisation

Three modes. `oidc` validates a JWT against the IdP's JWKS and maps group claims
onto roles; `static` uses hashed service API keys; `dev` trusts headers and is
**refused at startup** when `APP_ENV=prod`. Unsigned tokens (`alg: none`) are
rejected, and an algorithm needing asymmetric verification without PyJWT
installed is refused rather than accepted unverified.

Eleven roles map to fifteen permissions (`domain/enums.py`). A buyer cannot
approve technically; an engineer cannot release a PO; an auditor is read-only.

## Safety switches

Both default to off:

- `ALLOW_AUTOMATED_EMAIL_SEND` — otherwise a fully-rendered message is stored as
  `PENDING_APPROVAL` for human release.
- `ALLOW_AUTOMATED_PO_CREATION` — ProcureGuard emits a draft and an SAP-shaped
  payload; it does not write to the ERP.

## Data handling

Multi-tenant scoping is on every business table and is the leading column of the
hot indexes. Logs redact anything resembling a password, token, IBAN or account
number. The audit log is append-only with no update or delete method.

## Production requirements

Enterprise SSO with MFA, private networking, Secrets Manager, KMS CMKs with
rotation, S3 versioning and Object Lock for retention, malware scanning on
inbound attachments, DLP on outbound mail, CockroachDB audit logging, and **dual
control for any supplier bank-detail change** — verified by telephone with a known
contact, never from the email that requested it.
