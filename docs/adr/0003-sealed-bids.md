# ADR 0003: Sealed bids are encrypted, not flagged

Status: Accepted

## Context

Technical evaluation must not be influenced by price. The conventional
implementation is a visibility flag plus discipline in the application layer.

That is not sufficient here. The agent assembles model context from the same
tables, and a single missed check — or a prompt injection that persuades the model
to ask for a price — leaks the thing the control exists to prevent. A flag also
gives no protection against a buyer reading the table directly.

## Decision

While a case is technically sealed, each quotation's commercial payload is
encrypted at rest with a per-bid data key, and the plaintext columns hold no
prices at all. The encryption context binds the ciphertext to its case and
quotation identifiers.

Unsealing requires a recorded technical approval by a named human, and is itself
an audited event with the actor stored on every quotation.

## Consequences

- Price cannot influence technical judgement even if application code is wrong,
  because the data is not readable.
- A data key issued for one bid cannot open another; the test suite asserts that a
  mismatched encryption context fails.
- Technical evaluation must work from non-commercial content only, which is
  stricter but also correct: it forces requirement answers to be evaluated on
  their own terms.
- Production requires KMS. `Settings` refuses to start `APP_ENV=prod` with the
  local encryptor.

## Note on a failed variant

An earlier revision *also* blanked commercial fields on read as belt-and-braces.
Because that mutated the persistent ORM instance, assigning `lines = []` on a
delete-orphan relationship deleted the quotation lines at the next flush, and
commercial normalisation silently found nothing to compare.

Redaction is now a read-only projection. The lesson is specific: a defensive
measure that mutates persistent state is not defensive.
