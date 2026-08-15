# ADR 0004: The model extracts, deterministic code decides

Status: Accepted

## Context

Two different kinds of work look superficially similar in this pipeline:

1. *Locating* what a supplier offered against a requirement, in prose, in a table,
   or in a PDF that has been through three email clients.
2. *Deciding* whether the located value satisfies the requirement.

The first is genuinely hard for deterministic code and well suited to a language
model. The second is arithmetic.

A model asked "is 232 psi compliant with a 16 bar minimum?" will usually say yes.
It is 15.996 bar, so the answer is no — and a supplier who is 0.03% short on a
pressure rating has not met the specification.

## Decision

Every extraction stage runs a deterministic parser first and treats the model as
a supplement, asked only for what the parser did not find. Anything the model
contributes is stored with a capped confidence and marked with its source, so a
reviewer can always distinguish parsed from inferred.

Compliance itself is decided by `Requirement.evaluate()`: `Decimal` comparison
against an audited unit table, with affine-aware conversion. The model is never
asked whether something complies.

Three corollaries are enforced:

- Silence is `NOT_ADDRESSED`, and a mandatory `NOT_ADDRESSED` disqualifies.
- An assertion with no value ("fully compliant") is `UNVERIFIABLE`.
- A declared deviation blocks qualification until a named human accepts it
  against that specific requirement, with a reason.

## Consequences

- With `LLM_BACKEND=deterministic` the whole pipeline still runs, and its outputs
  are the parser's — reproducible by hand and by CI. This is why the test suite
  needs no model.
- Evaluations are replayable: given the stored inputs, the same figures come out.
- Recall on unusual prose is lower without a model. That is the accepted trade:
  a missed requirement surfaces as a gap for a human, whereas a wrongly-confirmed
  requirement surfaces as a defective part on a production line.
- The unit table is now load-bearing and must be maintained. It covers commercial
  order units and engineering units separately, because rejecting an unrecognised
  order unit protects a price while rejecting an unrecognised engineering unit
  would silently drop a requirement.
