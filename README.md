# ProcureGuard

**Live demo:** <http://ec2-3-86-224-51.compute-1.amazonaws.com> ·
**Tool map:** [docs/hackathon.md](docs/hackathon.md) ·
**Demo walkthrough:** [docs/demo-script.md](docs/demo-script.md)

The demo opens on a **How it works** tab: the path a materials requisition takes
through an engineering and procurement team today, the same path with
ProcureGuard, and what CockroachDB and AWS each carry. Then drop one of the
requisitions in [`samples/`](samples/) on the Cases tab and watch a real Temporal
workflow start.

A human-in-the-loop procurement agent for manufacturing companies. It takes a
purchase requisition and carries it to a ready-to-release purchase order:
validating it against SAP master data, researching what the company has paid
before, shortlisting suppliers, issuing RFQs, chasing replies, evaluating bids
technically and commercially, negotiating, and drafting the PO and the info
record.

**It never approves anything.** Four gates require an authenticated human, and
the deterministic policy layer — not a prompt — decides that they are required.

```
┌─ agent ────────────────────────────────────────────────────────────────────┐
│ 1 PR parser        4 document ingestion   7 RFQ generation   12 normalise  │
│ 2 material master  5 requirement extract  8 email in/out     13 L1/L2/L3   │
│ 3 price history    6 supplier shortlist   9 quote ingestion  14 negotiate  │
│                                          10 technical compare 15 PO draft  │
└────────────────────────────────────────────────────────────────────────────┘
        ▲ RFQ release   ▲ technical approval   ▲ negotiation   ▲ award + PO
        └───────────────┴── human gates (11) ──┴──────────────┘
```

## Why the design looks like this

**CockroachDB is the memory, and it is queried rather than loaded.** Millions of
PO lines, every document version and every asserted claim live in the database
permanently. Bounded, indexed queries return twelve rows of price history; the
model never sees a table. Memory that has to fit in a context window is not
memory — see [docs/hackathon.md](docs/hackathon.md).

**Sealed bids are encrypted, not merely flagged.** Until a human approves the
technical evaluation, each supplier's commercial payload is encrypted with a
per-bid data key bound to the case. The agent physically cannot read prices while
judging technical compliance — so price cannot influence that judgement even
under a prompt injection.

**Extraction may be probabilistic; decisions are not.** A model locates what a
supplier offered. Whether `232 psi` satisfies `minimum 16 bar` is decided by
Decimal arithmetic against an audited unit table. (It does not: 232 psi is
15.996 bar. The test suite asserts that.)

**Supplier content is hostile input.** Every document and email passes a firewall
that detects prompt injection, outcome steering, bank-detail fraud, competitor-
price exfiltration and lookalike sender domains — including Unicode and homoglyph
evasion. Quarantined content is stored and visible to humans but never reaches
the model.

**Silence is never compliance.** An unanswered mandatory requirement disqualifies
a bid. "Fully compliant" with no stated value is `UNVERIFIABLE`, not compliant.

## Quick start

Requires Docker and Python 3.12+.

```bash
make bootstrap     # venv, containers, migrations, seed, demo cases
make run           # API + approval UI on http://localhost:8000
```

Then open <http://localhost:8000>, pick an identity in the top-right, and work
the cases waiting for a decision.

Step by step instead:

```bash
make install                 # virtualenv and dependencies
make up                      # CockroachDB, Temporal, MinIO, MailHog
make migrate                 # create the schema
make seed SCALE=medium       # ~250k PO lines of synthetic history
make demo                    # three cases driven through all fifteen stages
make run                     # API and UI
make worker                  # Temporal worker, in a second terminal
```

## Scale

`make seed SCALE=...` generates a complete synthetic manufacturer. The history is
statistically realistic, not merely large: category-specific inflation, currency
drift, power-law quantity discounts, seasonal demand, persistent per-supplier
price and quality levels, and supplier churn.

| scale  | materials | vendors | PO lines  | years |
| ------ | --------: | ------: | --------: | ----: |
| tiny   |       120 |      25 |     2,000 |     2 |
| small  |       800 |      90 |    25,000 |     3 |
| medium |     3,500 |     260 |   180,000 |     5 |
| large  |    12,000 |     700 |   900,000 |     6 |
| xlarge |    30,000 |   1,500 | 3,000,000 |     7 |

Loading uses `COPY … FROM STDIN` in batches, then `ANALYZE` so the optimiser
plans against fresh statistics.

## Running without cloud credentials

Every external dependency has a port with two adapters. The default local
adapters need no AWS account, and the pipeline is identical:

| Port      | Production          | Local default                  |
| --------- | ------------------- | ------------------------------ |
| Database  | CockroachDB Cloud   | CockroachDB single node        |
| Documents | S3 + KMS            | Filesystem, content-addressed  |
| Model     | Bedrock             | Deterministic rule-based       |
| Vectors   | Bedrock Titan       | Hashed n-grams, same dimension |
| Email     | SES in/out          | `.eml` outbox, or MailHog      |
| Sealed    | KMS envelope        | AES-GCM-equivalent, app key    |
| Workflow  | Temporal Cloud      | Temporal dev server            |
| Identity  | OIDC / SSO          | `X-Actor-Id` headers           |

The language model is a **supplement**, not a dependency. Each extraction stage
runs a deterministic parser first and asks the model only for what the parser
missed. With `LLM_BACKEND=deterministic` the full pipeline still runs, and the
outputs are the parser's — reproducible by hand.

## Running with AWS

Optional — the pipeline is complete without it. To enable Amazon Bedrock:

```bash
# .env
LLM_BACKEND=bedrock
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_REGION=us-east-1
```

Two things catch people out:

- **`Settings` reads `.env`; boto3 does not.** boto3 walks its own credential
  chain and will otherwise pick up `~/.aws/credentials` from an unrelated
  project. The `run`, `worker`, `seed` and `demo` make targets export `.env`
  into the environment for this reason. Launching `uvicorn` by hand needs
  `set -a && . ./.env && set +a` first.
- **Current Anthropic models on Bedrock are inference-profile only.** Use the
  `us.` prefix, and grant the IAM principal `bedrock:InvokeModel` on the
  foundation-model ARNs in every region the profile can route to — us-east-1,
  us-east-2 and us-west-2 — not just the one you call. See
  [`infra/iam/procureguard-hackathon-access.json`](infra/iam/procureguard-hackathon-access.json).

Leaving `LLM_BACKEND=bedrock` set while Bedrock is unreachable is safe: every
model call site is guarded, logs the failure, and the stage proceeds on
deterministic parser output.

## Layout

```
procureguard/
  domain/           entities, state machine, policy, Money, units — no I/O
  application/      the fifteen pipeline stages
  ingestion/        deterministic parsers (PR, spec, quotation, text extract)
  infrastructure/   CockroachDB, S3, Bedrock, SES, KMS adapters
  workflows/        Temporal workflow and activities
  api/              REST API, RBAC, and the approval UI
  seed/             synthetic enterprise generator and bulk loader
```

## Verification

```bash
make test                                  # 90 unit tests, no database needed
PROCUREGUARD_TEST_DB=1 make test-integration   # 15 more against CockroachDB
```

The integration suite asserts the guarantees rather than the plumbing: that a
sealed bid holds no plaintext price in the database, that a buyer receives 403
on a technical approval, that a material is its own nearest vector neighbour,
and that a case reaches `ORDER_PLACED` through all fifteen stages.

## Safety defaults

`ALLOW_AUTOMATED_EMAIL_SEND=false` and `ALLOW_AUTOMATED_PO_CREATION=false` ship
off. The agent drafts a complete, rendered message and stores it as
`PENDING_APPROVAL`; a human with `EMAIL_SEND` releases it. ProcureGuard never
writes to SAP — it emits a draft PO and an SAP-shaped payload for a human to
release.

See [docs/hackathon.md](docs/hackathon.md) for the CockroachDB and AWS tool map,
[docs/architecture.md](docs/architecture.md),
[docs/security.md](docs/security.md), [docs/deployment.md](docs/deployment.md)
and the ADRs in [docs/adr/](docs/adr/).
