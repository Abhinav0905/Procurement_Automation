# Architecture

## Principle

CockroachDB is the agent's memory. The distinction that matters is between memory
that is *stored* and memory that is *loaded*: millions of purchase-order lines,
every document version and every asserted claim live in the database permanently,
and are reached only through bounded, indexed queries that return small typed
results. The model never receives a table.

This is what makes the memory production-grade rather than decorative. It
survives restarts, outlives any context window, carries provenance for every
fact, and is queried the same way at three rows or three million.

```text
SAP exports ──► S3 ──► importer ──────────► CockroachDB enterprise tables
Documents ────► S3 ──► extract ─► FIREWALL ─► CockroachDB evidence tables
                                              │
Temporal ◄──► application services ◄──────────┘
                    │
                    ├──► Amazon Bedrock (bounded reasoning only)
                    ├──► SES (gated by policy)
                    └──► KMS (sealed commercial bids)
```

## The three data classes

### Enterprise relational — the SAP mirror

Material master and plant extensions, vendor master and contacts, source list,
purchasing info records, framework contracts, PO history, goods-receipt history,
FX and freight rates. Append-only, bitemporal (`valid_from` / `valid_to`) so an
overlapping re-export supersedes rows rather than destroying them.

`purchase_history` is the largest table and deliberately carries no foreign keys:
it is an append-only mirror loaded by `COPY`, and the importer guarantees its
integrity. Its workhorse index answers "what did we last pay for this material?"
with a covering index on `(tenant_id, material_code, order_date)`.

### Evidence knowledge — immutable and provenanced

Documents, immutable versions keyed by content hash, retrievable chunks with
embeddings, and atomic subject-predicate-value claims. Every claim records which
document version it came from, who asserted it, how much it should be trusted and
what it superseded. Conflicting claims are surfaced, and an ERP or engineering
statement automatically outranks a supplier assertion.

Nothing here is updated in place. A change is a new version or a new claim.

### Agent state — the durable case file

Cases, requisitions, requirements, shortlists with full score breakdowns, RFQs
and invitations, quotations, the compliance matrix, normalised offers, bid
rankings, negotiation rounds, approvals, communications, decisions with evidence,
reminders and the audit log.

## Ports and adapters

Application services depend on protocols in `procureguard/ports`, never on
concrete infrastructure. Each port has a cloud adapter and a local adapter, wired
by one composition root (`infrastructure/factory.py`). That is what lets the
complete pipeline run in CI with no credentials while production keeps identical
code paths.

## The Temporal boundary

Temporal decides *when* an activity may run, sleeps durably for weeks, retries
transient failures, and waits for human signals. It contains no business rules.

Activities are invoked by string name so no database or network code is imported
into the workflow determinism sandbox. Two rules the activity layer obeys, both
because Temporal delivers at least once:

- Outward side effects (email, ERP writes) are claimed against an idempotency key
  *before* transmission, so a retry after a lost acknowledgement cannot double-send.
- No database transaction is held across a remote call.

Retryable infrastructure failures back off; a policy refusal or validation error
is a decision and surfaces immediately via `non_retryable_error_types`.

## CockroachDB specifics

- **Serializable by default.** Contended transactions abort with SQLSTATE 40001.
  That is the concurrency-control protocol, not an error path, so every
  contending write goes through `run_in_transaction` with full-jitter backoff.
- **UUID primary keys** generated in the application. Monotonic sequences would
  hot-spot a single leaseholder.
- **Native vector search.** CockroachDB 25.2+ provides `VECTOR(n)` with C-SPANN
  ANN indexes. Capability is probed once at engine creation and the embedding
  column resolves to `VECTOR(n)` or `JSONB` accordingly; both paths return the
  same ordered results.
- **Dialect.** `sqlalchemy-cockroachdb`, because the stock PostgreSQL dialect
  cannot parse CockroachDB's `version()` string.

Two behaviours differ from PostgreSQL and are handled explicitly:
`percentile_cont` requires a `FLOAT8` ordering column, and `COALESCE` will not
unify a `DECIMAL` column with an `INT` literal.

## Retrieval

Hybrid: vector recall merged with keyword precision by reciprocal rank fusion.
Pure vector search misses exact part numbers and standard designations — `ASME
B16.34` — which is precisely what a technical evaluation must find. Quarantined
chunks are excluded from retrieval by default.

## Where the money maths lives

`domain/money.py` and `domain/units.py` are pure and have no I/O. Money is always
`Decimal`; arithmetic across currencies raises rather than coercing; FX needs an
explicit as-of date. Unit conversion is affine-aware, so Celsius and Fahrenheit
convert correctly and `factor()` refuses to return a single multiplier for them.

Cross-dimension conversion (`BOX` → `EA`) requires a material-master alternate
unit. Refusing to guess is deliberate: a silently wrong unit corrupts every
downstream price comparison.

## Production rule

Never hold a database transaction open while calling a language model, an email
service, or any other remote dependency.
