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

### 2. Cloud Managed MCP Server

**Where:** [`.mcp.json`](../.mcp.json)

The Managed MCP Server gives an agent a direct, governed channel to the cluster —
schema exploration, index recommendations, query plans and read-only SQL —
without a second data-plane path. Access runs through CockroachDB Cloud's own
authentication and RBAC rather than a bespoke credential.

```json
{
  "mcpServers": {
    "cockroachdb-cloud": {
      "type": "http",
      "url": "https://cockroachlabs.cloud/mcp",
      "headers": { "mcp-cluster-id": "<cluster-id>" }
    }
  }
}
```

Authenticate with `/mcp` in Claude Code, then choose the permission level.

**We deliberately grant read-only.** The application writes through its own
connection, where the deterministic policy layer and the four human approval
gates sit in the path of every mutation. An agent channel that could write to
`purchase_requisitions` or `approvals` would route around exactly the controls
this system exists to enforce. Read-only MCP gives an agent full visibility into
the memory layer while leaving mutation to the audited path — which is the same
principle as sealed bids and `ALLOW_AUTOMATED_PO_CREATION=false`.

### 3. ccloud CLI

**Where:** [`infra/ccloud/provision.sh`](../infra/ccloud/provision.sh)

The cluster's control plane is scripted, not clicked. `ccloud` creates the
cluster, creates the database, resolves the connection string and fetches the CA
certificate, so the deployment is reproducible from a shell:

```bash
brew install cockroachdb/tap/ccloud
ccloud auth login
./infra/ccloud/provision.sh
```

The cluster is **BASIC on AWS `us-east-1`** — the same cloud and region as the
Bedrock endpoint the agent calls, so the memory layer and the model sit next to
each other rather than across a public hop.

#### One operational note worth recording

The baseline migration fails against a BASIC cluster with the application's
default 30s statement timeout:

```
psycopg.errors.QueryCanceled: query execution canceled due to statement timeout
```

DDL over a network connection to a throttled cluster exceeds a timeout that is
correct for serving traffic. Raise it for the migration only:

```bash
DB_STATEMENT_TIMEOUT_MS=600000 make migrate
```

The baseline is `metadata.create_all(checkfirst=True)`, so a partially applied
migration resumes cleanly rather than needing a teardown.

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

- **Not deployed.** The Terraform describing the ECS topology is written but
  unapplied; the running demo is the Cloud cluster plus a local application
  process.
- **Two clusters, deliberately.** The CockroachDB Cloud cluster carries the
  MCP-connected demo at `small` scale. The local single-node cluster holds the
  180k-line history used for the scale and benchmark figures, because seeding
  that volume across a network into a BASIC cluster is slow and proves nothing
  extra. Both run identical schema and identical code; `DATABASE_URL` selects.
- **Embeddings are lexical, not semantic, by default.** `EMBEDDING_BACKEND=hashing`
  uses hashed word and character n-grams — deterministic, free and offline, and
  genuinely effective for part descriptions, but it does not know that "SS" means
  stainless steel. `EMBEDDING_BACKEND=bedrock` switches to Titan. Note that
  `EMBEDDING_DIMENSIONS` is part of the schema: moving from 256 to 1024 requires
  re-embedding every indexed row.
