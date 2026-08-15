# CockroachDB × AWS Hackathon — tools used

This document maps the hackathon requirements onto the code, so each claim can be
checked rather than taken on trust. Every command below is runnable against a
local stack brought up with `make up`.

## CockroachDB tools

### 1. Distributed Vector Indexing

**Where:** [`procureguard/infrastructure/db/vector.py`](../procureguard/infrastructure/db/vector.py),
[`migrations/versions/0001_baseline.py`](../migrations/versions/0001_baseline.py)

CockroachDB's native `VECTOR(n)` type with C-SPANN ANN indexes carries all
semantic retrieval in the system. Capability is probed once when the engine is
created; the embedding column resolves to `VECTOR(n)` when the cluster supports
it and `JSONB` otherwise, so the same code runs on an older cluster with reduced
latency rather than failing to start.

Three tables carry embeddings, each with an ANN index:

| Table | Column | Index | Purpose |
| --- | --- | --- | --- |
| `materials` | `embedding` | `idx_material_embedding_ann` | Match free-text requisition lines to SAP material master |
| `vendors` | `embedding` | `idx_vendor_embedding_ann` | Surface suppliers by capability, not just purchase history |
| `document_chunks` | `embedding` | `idx_chunk_embedding_ann` | Retrieve the clause of a supplier spec that answers a requirement |

Verify:

```bash
procureguard db check          # -> {"vector_backend": "native"}

# native VECTOR columns, not JSONB
docker exec procureguard-crdb ./cockroach sql --insecure -d procureguard -e "
  SELECT table_name, column_name, data_type FROM information_schema.columns
  WHERE table_schema='public' AND column_name='embedding';"

# the ANN indexes
docker exec procureguard-crdb ./cockroach sql --insecure -d procureguard -e "
  SELECT table_name, index_name FROM information_schema.statistics
  WHERE index_name LIKE '%_ann';"
```

Search is **hybrid**, not vector-only. Vector recall is merged with keyword
precision by reciprocal rank fusion
([`document_ingestion.py`](../procureguard/application/document_ingestion.py)),
because pure vector search reliably misses exact part numbers and standard
designations — `ASME B16.34` — which is precisely what a technical evaluation
must find. A chunk retrieved by both methods accumulates score from each and is
tagged `hybrid`.

### 2. _(pending)_ Cloud Managed MCP Server

Not yet wired. See "Known gaps" below.

## AWS services

### Amazon Bedrock

**Where:** [`procureguard/infrastructure/llm/bedrock.py`](../procureguard/infrastructure/llm/bedrock.py)

Bedrock runs the extraction stages that a deterministic parser cannot fully
cover: requirement extraction, compliance location, and negotiation drafting.
Model calls return a `ModelResponse` carrying model id, token counts, latency and
guardrail status, so every model interaction is auditable rather than anonymous.

The model is a **supplement, not a dependency**. Each stage runs a deterministic
parser first and asks the model only for the remainder, and every call site is
guarded — if Bedrock is unreachable the stage logs the failure and proceeds on
parser output. `LLM_BACKEND=deterministic` runs the entire fifteen-stage pipeline
with no model and no AWS credentials, which is how CI runs it.

### IAM (least privilege)

**Where:** [`infra/iam/`](../infra/iam/)

The credential this project runs under is scoped to Bedrock invoke and S3 on
`procureguard-*` buckets in a single region. It cannot create EC2, ECS, RDS or
VPC resources at all, so the maximum attainable spend is bounded by capability
rather than by trust. A companion deny-all policy is attached automatically by
AWS Budgets if spend crosses a threshold.

### Described in Terraform, not deployed

[`infra/terraform/`](../infra/terraform/) provisions the production topology:
ECS Fargate for API and workers, ALB, S3 with versioning and Object Lock, KMS
CMKs for sealed bids, Secrets Manager, SES receipt rules, and least-privilege
task roles. It is included as evidence of the production design; a hackathon
demo does not need a NAT gateway.

## Agentic memory design

Memory here is not a transcript buffer. It is four distinct stores, each with
different durability and trust semantics:

| Store | Contents | Property |
| --- | --- | --- |
| **Enterprise history** | 180k+ PO lines, vendor and material master, info records, FX | Bitemporal, append-only; re-import supersedes rather than destroys |
| **Evidence** | Documents, immutable content-hashed versions, chunks, atomic claims | Never updated in place; a change is a new version or a new claim |
| **Case state** | The full sourcing case: requirements, shortlists, bids, rankings, approvals | Durable across weeks; Temporal resumes mid-case after a deploy |
| **Semantic index** | Embeddings over materials, vendors, document chunks | Recall path into the three stores above |

Two properties matter more than volume:

**Memory is queried, never loaded.** Millions of PO lines stay in CockroachDB.
A benchmark request returns twelve rows through a covering index on
`(tenant_id, material_code, order_date)`. The model never sees a table.

**Memory carries provenance.** Every claim records the document version it came
from, who asserted it, how far it should be trusted, and what it superseded.
Conflicting claims are surfaced rather than silently resolved, and an ERP
statement automatically outranks a supplier assertion. A decision can be replayed
years later against the exact evidence that produced it.

## Known gaps

Stated plainly rather than left for a judge to discover:

- **Second CockroachDB tool.** Only Distributed Vector Indexing is wired today.
  The Managed MCP Server requires a CockroachDB Cloud cluster; the local Docker
  cluster cannot serve it.
- **Not deployed.** The Terraform is written but unapplied.
- **Embeddings are lexical, not semantic, by default.** `EMBEDDING_BACKEND=hashing`
  uses hashed word and character n-grams — deterministic, free and offline, and
  genuinely effective for part descriptions, but it does not know that "SS" means
  stainless steel. `EMBEDDING_BACKEND=bedrock` switches to Titan. Note that
  `EMBEDDING_DIMENSIONS` is part of the schema: moving from 256 to 1024 requires
  re-embedding every indexed row.
