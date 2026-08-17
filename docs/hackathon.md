# CockroachDB × AWS Hackathon — tools used

**Live demo:** <http://ec2-54-204-112-30.compute-1.amazonaws.com>
(pick an identity in the top-right, then work a case)

This document maps the hackathon requirements onto the code, so each claim can be
checked rather than taken on trust. Every command below is runnable against a
local stack brought up with `make up`.

## Verified end to end

Against the deployed instance and the CockroachDB Cloud cluster behind it:

| Check | Result |
| --- | --- |
| `health/ready` | `status: ok`, CockroachDB Cloud node, `vector_backend: native` |
| Schema | 46 tables; 3 native `vector` columns; 3 C-SPANN ANN indexes |
| Data | 800 materials, 90 vendors, 25,000 PO lines |
| Pipeline | 3 cases through all 15 stages to `ORDER_PLACED`, suppliers awarded |
| Audit | 18 approvals, 57 decisions, 98 audit entries |
| Semantic search | `stainless ball valve` → `Ball valve DN98 SS 316L` @ 0.3642 |
| RBAC | a `BUYER` posting a technical approval gets `403 lacks TECHNICAL_APPROVE` |

`health/ready` reports `temporal: unavailable` on the demo box, and that is
working as designed: the readiness endpoint reports every dependency separately
so a degraded one is *visible* rather than inferred. The demo box runs the API and
approval UI only — the fifteen-stage pipeline is driven by `procureguard demo`,
which needs no Temporal. Temporal owns durable scheduling for live cases, which a
public demo does not exercise.

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

### Amazon EC2

**Where:** [`infra/ec2/deploy.sh`](../infra/ec2/deploy.sh)

The public demo runs on a single `t4g.micro` in `us-east-1`, configured entirely
from user-data: no ALB, no NAT gateway, no task definitions. A demo for a handful
of visitors does not need $99/month of networking, and the production topology is
already described in Terraform for anyone who wants to see it.

Two properties are deliberate rather than lazy:

- **The instance holds no AWS credentials.** It runs
  `LLM_BACKEND=deterministic`, so its only secret is the database URL. Someone
  who compromises the demo box gets an app and a Basic cluster — not the Bedrock
  key.
- **No inbound SSH.** The box is built from user-data, so there is no
  administrative surface to defend. Port 80 is the entire attack surface.

The AMI is resolved through SSM (`/aws/service/ami-amazon-linux-latest/...`)
rather than pinned, and the IAM policy carries an explicit `Deny` on
`ec2:RunInstances` for anything outside `t4g.nano|micro|small`, so the blast
radius of a mistake is bounded at roughly $12/month.

Teardown is one command: `./infra/ec2/deploy.sh --terminate`

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

- **The ECS topology is described, not applied.** The running demo is a single
  `t4g.micro` against the Cloud cluster. [`infra/terraform/`](../infra/terraform/)
  shows how this would actually be deployed — multi-AZ Fargate, ALB, KMS, SES —
  and is included as design evidence rather than a running system.
- **Bedrock is coded and documented but gated.** Anthropic models on Bedrock
  require a per-account use-case form; until it clears, every model call site
  logs the failure and the stage proceeds on deterministic parser output. That is
  the designed behaviour, not a workaround — see the guard in
  [`requirements.py`](../procureguard/application/requirements.py).
- **`POST /cases/{id}/requirements/extract` is not idempotent.** Re-running it on
  a case that already has requirements raises a `UniqueViolation` on
  `uq_requirement_key` and returns 500. It should supersede the existing active
  requirements rather than insert alongside them.
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
